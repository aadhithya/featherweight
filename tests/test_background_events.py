"""Background jobs: the events on a chat stream, cancelling the children of a run that was stopped, and the trace."""

import asyncio
import json
from typing import ClassVar

import pytest
from ag_ui.core import CustomEvent, UserMessage
from ag_ui.encoder import EventEncoder
from fakes import FakeSettings, Hold, Scripted, Streams, runner_with, tool_call_chunk, tool_reply
from fakes import structured_reply as reply
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from featherweight.adapters import vercel
from featherweight.client import JobClient
from featherweight.jobs import JobContext, job
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, RunStatus
from featherweight.otel import OTelObserver
from featherweight.runner import AgentRunner
from featherweight.streaming import ChatInput, StreamingAgent
from featherweight.testing import MemoryQueue, MemoryResultStore
from featherweight.worker import JobWorker


class ChildInput(AgentInput):
    topic: str


class ChildOutput(AgentOutput):
    summary: str | None = None


GATES: dict[str, Hold] = {}  # a job that a test holds, by name


@job
async def quick(ctx: JobContext, input: ChildInput) -> ChildOutput:
    return ChildOutput(summary="the secret result")


@job
async def slow(ctx: JobContext, input: ChildInput) -> ChildOutput:
    await GATES["slow"].wait()
    return ChildOutput(summary="late")


class Chat(StreamingAgent):
    name: str = "chat"
    description: str = "Talks to the user, and starts agents in the background"
    version: str = "1"
    prompt: str = "You are a helpful chat agent."
    model: ModelSettings = FakeSettings()
    background_jobs: list[type] = [quick, slow]


def say(text: str = "go") -> ChatInput:
    return ChatInput(messages=[UserMessage(id="m1", content=text)], thread_id="t-1")


def start(name: str, call_id: str = "t1"):
    return tool_call_chunk("start_background", {"agent_name": name, "input_json": '{"topic": "x"}'}, call_id)


@pytest.fixture
def results():
    return MemoryResultStore()


@pytest.fixture(autouse=True)
def gates():
    GATES.clear()
    GATES["slow"] = Hold()
    yield
    GATES["slow"].release.set()  # a test that ends with the child held must not leave it waiting


async def reading(run) -> tuple[asyncio.Task, list]:
    events: list = []

    async def read() -> None:
        async for event in run:
            events.append(event)

    return asyncio.create_task(read()), events


async def children_done(runner: AgentRunner) -> None:
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)


def tool_result(model) -> str:
    """The text of the first tool result that the model was shown."""
    for messages in model.seen:
        for message in messages:
            if isinstance(message, ToolMessage):
                return message.content
    raise AssertionError("the model was shown no tool result")


def customs(events) -> list[CustomEvent]:
    return [e for e in events if isinstance(e, CustomEvent)]


async def a_turn_that_started(db, results, agent_name, **runner_args):
    """A streamed turn that starts `agent_name` in the background and then holds, so that the test can act while it runs."""
    hold = Hold()
    model = Streams(turns=[[start(agent_name)], [hold, "done"]])
    runner = runner_with(db, model, results=results, **runner_args)
    run = runner.stream(Chat, say())
    reader, events = await reading(run)
    await asyncio.wait_for(hold.reached.wait(), timeout=10)  # the tool has run: the child is started
    return runner, run, hold, reader, events


# the events on the stream


async def test_a_stream_announces_a_child_and_its_end_with_metadata_only(db, results):
    runner, run, hold, reader, events = await a_turn_that_started(db, results, "quick")
    await children_done(runner)
    await asyncio.sleep(0.1)  # the watcher of the child sees its job end
    hold.release.set()
    await asyncio.wait_for(reader, timeout=10)

    announced = customs(events)
    assert [e.name for e in announced] == ["background_started", "background_finished"]
    started, finished = announced
    assert set(started.value) == {"job_id", "agent"} and started.value["agent"] == "quick"
    assert finished.value == {
        "job_id": started.value["job_id"],
        "agent": "quick",
        "status": "SUCCESS",
        "error_type": None,
    }
    assert "secret" not in json.dumps([e.model_dump(mode="json") for e in announced])  # the result is not in an event
    kinds = [e.type.value for e in events]
    assert kinds.index("CUSTOM") < kinds.index("RUN_FINISHED") and len(kinds) - 1 - kinds[::-1].index(
        "CUSTOM"
    ) < kinds.index("RUN_FINISHED")


async def test_the_events_go_out_over_both_adapters(db, results):
    runner, run, hold, reader, events = await a_turn_that_started(db, results, "quick")
    await children_done(runner)
    await asyncio.sleep(0.1)
    hold.release.set()
    await asyncio.wait_for(reader, timeout=10)

    encoder = EventEncoder()
    wire = "".join(encoder.encode(e) for e in events)
    assert "background_started" in wire and "background_finished" in wire  # AG-UI: as they are

    parts = vercel.translate(events)
    started = [p for p in parts if "data-background_started" in p]
    finished = [p for p in parts if "data-background_finished" in p]
    assert len(started) == 1 and len(finished) == 1
    assert '"transient":true' in started[0] and '"agent":"quick"' in started[0]


def test_the_vercel_adapter_turns_a_custom_event_into_a_transient_data_part():
    event = CustomEvent(name="background_started", value={"job_id": "j1", "agent": "quick"})
    assert vercel.Translator().feed(event) == [
        {"type": "data-background_started", "data": {"job_id": "j1", "agent": "quick"}, "transient": True}
    ]


async def test_nothing_is_announced_after_the_stream_ended_and_the_child_carries_on(db, results):
    runner, run, hold, reader, events = await a_turn_that_started(db, results, "slow")
    hold.release.set()  # the turn ends on its own, with the child still running
    await asyncio.wait_for(reader, timeout=10)
    started = customs(events)
    assert [e.name for e in started] == ["background_started"]
    job_id = started[0].value["job_id"]
    assert (await db.get_job(job_id)).status == RunStatus.RUNNING
    assert runner._emitters == {} and runner._watchers == {}

    GATES["slow"].release.set()
    await children_done(runner)
    assert (await db.get_job(job_id)).status == RunStatus.SUCCESS
    assert [e.name for e in customs(events)] == ["background_started"]  # no `background_finished`: the stream is over


# cancelling


async def test_stopping_the_response_cancels_a_child_that_runs_here(db, results):
    runner, run, hold, reader, events = await a_turn_that_started(db, results, "slow")
    job_id = customs(events)[0].value["job_id"]
    await asyncio.wait_for(GATES["slow"].reached.wait(), timeout=10)

    run.cancel()
    await asyncio.wait_for(reader, timeout=10)

    row = await db.get_job(job_id)
    assert row is not None and row.status == RunStatus.CANCELLED
    assert [e.type for e in await db.list_job_events(job_id)][-1] == "cancelled"
    assert [e.name for e in customs(events)] == [
        "background_started"
    ]  # and the stream announces nothing after it ended
    assert runner._children == {} and runner._emitters == {}


async def test_stopping_the_response_cancels_a_child_on_a_worker(db, results):
    queue = MemoryQueue()
    client = JobClient(db, queue, results)
    worker = JobWorker(
        db,
        queue,
        AgentRunner(db, results=results),
        jobs=[slow],
        results=results,
        receive_timeout=0.05,
        cancel_poll=0.05,
    )
    running = asyncio.create_task(worker.run())
    try:
        runner, run, hold, reader, events = await a_turn_that_started(db, results, "slow", background=client)
        job_id = customs(events)[0].value["job_id"]
        await asyncio.wait_for(GATES["slow"].reached.wait(), timeout=10)  # the worker runs it

        run.cancel()
        await asyncio.wait_for(reader, timeout=10)
        row = await client.wait(job_id, timeout=10)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)

    assert row.status == RunStatus.CANCELLED
    assert "cancel_requested" in [e.type for e in await db.list_job_events(job_id)]


class Worker(AgentBase):
    name: str = "worker"
    description: str = "Starts an agent in the background, then waits for a long tool"
    version: str = "1"
    prompt: str = "Do the task."
    model: ModelSettings = FakeSettings()
    background_jobs: list[type] = [slow]
    input_model: ClassVar[type[AgentInput]] = ChildInput
    output_model: ClassVar[type[AgentOutput]] = ChildOutput


async def test_cancelling_a_run_that_is_not_a_chat_cancels_its_children(db, results):
    hanging = Hold()

    @tool
    async def hang() -> str:
        """Wait for a long time."""
        await hanging.wait()
        return "never"

    class Hanging(Worker):
        name: str = "hanging"
        tools: list = [hang]

    model = Scripted(
        script=[
            tool_reply("start_background", {"agent_name": "slow", "input_json": '{"topic": "x"}'}, "t1"),
            tool_reply("hang", {}, "t2"),
            reply("ChildOutput", {}),
        ]
    )
    runner = runner_with(db, model, results=results)
    task = asyncio.create_task(runner.run(Hanging, ChildInput(topic="x")))
    await asyncio.wait_for(hanging.reached.wait(), timeout=10)
    await asyncio.wait_for(GATES["slow"].reached.wait(), timeout=10)
    (child,) = [c for children in runner._children.values() for c in children]

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    row = await db.get_job(child.job_id)
    assert row is not None and row.status == RunStatus.CANCELLED
    assert runner._children == {}


# the trace


def ancestors(spans, span):
    by_id = {s.context.span_id: s for s in spans}
    found = []
    parent = span.parent
    while parent is not None and parent.span_id in by_id:
        found.append(by_id[parent.span_id].name)
        parent = by_id[parent.span_id].parent
    return found


@pytest.fixture
def telemetry():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return OTelObserver(tracer_provider=provider), exporter


async def test_the_job_of_a_child_that_runs_here_is_under_the_tool_call(db, results, telemetry):
    observer, exporter = telemetry
    model = Scripted(
        script=[
            tool_reply("start_background", {"agent_name": "quick", "input_json": '{"topic": "x"}'}, "t1"),
            reply("ChildOutput", {}),
        ]
    )

    class Quick(Worker):
        name: str = "quicker"
        background_jobs: list[type] = [quick]

    runner = runner_with(db, model, results=results, observers=[observer])
    await runner.run(Quick, ChildInput(topic="x"))
    await children_done(runner)

    spans = list(exporter.get_finished_spans())
    child = next(s for s in spans if s.name == "invoke_workflow quick")
    tool_span = next(s for s in spans if s.name == "execute_tool start_background")
    assert child.context.trace_id == tool_span.context.trace_id
    assert "execute_tool start_background" in ancestors(spans, child)


async def test_the_job_of_a_child_on_a_worker_is_under_the_tool_call(db, results, telemetry):
    observer, exporter = telemetry
    queue = MemoryQueue()
    client = JobClient(db, queue, results, observers=[observer])
    worker_runner = AgentRunner(db, results=results, observers=[observer])
    worker = JobWorker(db, queue, worker_runner, jobs=[quick], results=results, receive_timeout=0.05)
    running = asyncio.create_task(worker.run())
    model = Scripted(
        script=[
            tool_reply("start_background", {"agent_name": "quick", "input_json": '{"topic": "x"}'}, "t1"),
            reply("ChildOutput", {}),
        ]
    )

    class Quick(Worker):
        name: str = "quicker"
        background_jobs: list[type] = [quick]

    try:
        runner = runner_with(db, model, results=results, observers=[observer], background=client)
        await runner.run(Quick, ChildInput(topic="x"))
        child_id = json.loads(tool_result(model))["job_id"]
        await client.wait(child_id, timeout=10)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)

    spans = list(exporter.get_finished_spans())
    child = next(s for s in spans if s.name == "invoke_workflow quick")
    assert "execute_tool start_background" in ancestors(spans, child)
