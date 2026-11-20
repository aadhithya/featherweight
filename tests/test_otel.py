"""OTelObserver: the spans, their parents and attributes, content on and off, and the trace context. Fake models, and the
OpenTelemetry SDK's in-memory exporter."""

import asyncio
import json

import pytest
from fakes import Echo, EchoInput, Scripted, Streams, run_pipeline, runner_with, structured_reply, use_model
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from test_job_observers import In, Out, Shout
from test_resume import model, steps
from test_streaming import Chat, say
from test_subagents import PARENT_INPUT, Parent, child_done, launch_child, parent_done

from featherweight import AgentRunner, JobClient, JobWorker, OTelObserver, SqliteQueue, run_job
from featherweight.events import wait_for_job
from featherweight.jobs import Job, JobContext
from featherweight.models import RunStatus
from featherweight.observers import JobEnd, JobStart, JobSubmitted
from featherweight.otel import ATTRIBUTES, CAPTURE_CONTENT_ENV, CORE_CONVENTIONS, GENAI_CONVENTIONS
from featherweight.testing import MemoryResultStore, check_observer

ECHO = EchoInput(text="hello")
CONTENT = {
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
}


def echoed(text="hello"):
    return structured_reply("EchoOutput", {"echoed": text})


@pytest.fixture
def telemetry():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def observer_for(telemetry, **kwargs) -> OTelObserver:
    return OTelObserver(tracer_provider=telemetry[0], **kwargs)


def spans_of(telemetry):
    return list(telemetry[1].get_finished_spans())


def edges(spans) -> list[tuple[str, str | None]]:
    """(name, the name of its parent) for every span, sorted."""
    by_id = {s.context.span_id: s.name for s in spans}
    return sorted((s.name, by_id.get(s.parent.span_id) if s.parent else None) for s in spans)


def named(spans, name):
    return [s for s in spans if s.name == name]


async def in_a_job(observer, body, **start):
    """What the runner will do for a job: the scope around the work."""
    job = JobStart(job_id="j1", name="pipe", version="2", job_hash="a" * 64, **start)
    async with observer.job(job) as scope:
        result = await body()
        await scope.finish(JobEnd(status=RunStatus.SUCCESS))
    return result


# the span tree


async def test_a_job_holds_its_attempts_and_a_launched_agent_hangs_under_its_tool_call(db, telemetry):
    observer = observer_for(telemetry)
    script = [launch_child("t-launch"), child_done(), parent_done()]
    runner = runner_with(db, Scripted(script=script), observers=[observer])

    result = await in_a_job(observer, lambda: runner.run(Parent, PARENT_INPUT))

    assert result.status == RunStatus.SUCCESS
    assert edges(spans_of(telemetry)) == [
        ("chat fake-model", "invoke_agent child"),
        ("chat fake-model", "invoke_agent parent"),
        ("chat fake-model", "invoke_agent parent"),
        ("execute_tool launch_agent", "invoke_agent parent"),
        ("invoke_agent child", "execute_tool launch_agent"),
        ("invoke_agent parent", "invoke_workflow pipe"),
        ("invoke_workflow pipe", None),
    ]
    spans = spans_of(telemetry)
    assert {s.name: s.kind for s in spans} == {
        "chat fake-model": SpanKind.CLIENT,
        "execute_tool launch_agent": SpanKind.INTERNAL,
        "invoke_agent child": SpanKind.INTERNAL,
        "invoke_agent parent": SpanKind.INTERNAL,
        "invoke_workflow pipe": SpanKind.INTERNAL,
    }
    assert len({s.context.trace_id for s in spans}) == 1  # one trace


async def test_the_spans_carry_the_conventions_attributes_and_ours(db, telemetry):
    observer = observer_for(telemetry)
    runner = runner_with(db, Scripted(script=[echoed()]), observers=[observer])

    await in_a_job(observer, lambda: runner.run(Echo, ECHO), resumed_from="old")

    spans = spans_of(telemetry)
    job = dict(named(spans, "invoke_workflow pipe")[0].attributes)
    assert job == {
        "gen_ai.operation.name": "invoke_workflow",
        "gen_ai.agent.name": "pipe",
        "ftw.job.id": "j1",
        "ftw.job.name": "pipe",
        "ftw.job.version": "2",
        "ftw.job.hash": "a" * 64,
        "ftw.resumed_from": "old",
    }
    (row,) = await db.list_runs()
    agent = dict(named(spans, "invoke_agent echo")[0].attributes)
    assert agent["gen_ai.operation.name"] == "invoke_agent" and agent["gen_ai.agent.name"] == "echo"
    assert agent["gen_ai.agent.version"] and agent["gen_ai.request.model"] == "fake-model"
    assert (agent["ftw.run.id"], agent["ftw.invocation.id"]) == (row.agent_run_id, row.invocation_id)
    assert (agent["ftw.attempt"], agent["ftw.attempts"], agent["ftw.attempt.final"]) == (1, 3, True)
    assert (agent["ftw.tokens.input"], agent["ftw.tokens.output"]) == (5, 7)
    assert agent["ftw.agent.hash"] == row.agent_hash and "ftw.job.id" not in agent  # the run had no job id
    chat = dict(named(spans, "chat fake-model")[0].attributes)
    assert chat["gen_ai.operation.name"] == "chat" and chat["gen_ai.provider.name"] == "fake"
    assert chat["gen_ai.request.model"] == "fake-model" and chat["gen_ai.response.model"] == "fake-model"
    assert (chat["gen_ai.usage.input_tokens"], chat["gen_ai.usage.output_tokens"]) == (5, 7)
    assert all(s.status.status_code == StatusCode.UNSET for s in spans)


async def test_every_attribute_that_is_emitted_is_in_the_list(db, telemetry):
    observer = observer_for(telemetry, capture_content=True)
    script = [launch_child("t-launch"), child_done(), parent_done()]
    runner = runner_with(db, Scripted(script=script), observers=[observer])
    await in_a_job(observer, lambda: runner.run(Parent, PARENT_INPUT), input={"task": "go"})
    first = await run_pipeline(db, "p", steps(runner_with(db, model(), observers=[observer]), ["a"]), resumable=True)
    await run_pipeline(db, "p", steps(runner_with(db, model(), observers=[observer]), ["a"]), resume_from=first.job_id)

    emitted = {key for s in spans_of(telemetry) for key in (s.attributes or {})}

    assert emitted <= ATTRIBUTES
    assert CONTENT <= emitted and "ftw.reused" in emitted and "ftw.resumed_from" in emitted


def test_the_version_of_the_conventions_is_pinned():
    assert GENAI_CONVENTIONS.startswith("semantic-conventions-genai@") and len(GENAI_CONVENTIONS.split("@")[1]) == 40
    assert CORE_CONVENTIONS.count(".") == 2
    assert all(name.startswith(("gen_ai.", "ftw.", "error.")) for name in ATTRIBUTES)


# retries, rejections and the attempt that counted


async def test_retries_are_siblings_that_share_an_invocation_and_only_the_last_is_final(db, telemetry):
    observer = observer_for(telemetry)
    runner = runner_with(db, Scripted(script=[RuntimeError("down"), echoed()]), observers=[observer], max_attempts=2)

    await in_a_job(observer, lambda: runner.run(Echo, ECHO))

    attempts = sorted(named(spans_of(telemetry), "invoke_agent echo"), key=lambda s: s.attributes["ftw.attempt"])
    first, second = attempts
    assert [s.parent.span_id for s in attempts] == [
        named(spans_of(telemetry), "invoke_workflow pipe")[0].context.span_id
    ] * 2
    assert first.attributes["ftw.invocation.id"] == second.attributes["ftw.invocation.id"]
    assert first.attributes["ftw.attempt.final"] is False and second.attributes["ftw.attempt.final"] is True
    assert first.status.status_code == StatusCode.ERROR and first.attributes["error.type"] == "RuntimeError"
    assert second.status.status_code == StatusCode.UNSET and "error.type" not in second.attributes


async def test_an_error_has_a_type_and_never_its_text(db, telemetry):
    observer = observer_for(telemetry, capture_content=True)
    runner = runner_with(
        db, Scripted(script=[RuntimeError("secret client text")]), observers=[observer], max_attempts=1
    )

    result = await runner.run(Echo, ECHO)

    assert result.status == RunStatus.FAILED
    (attempt,) = named(spans_of(telemetry), "invoke_agent echo")
    assert attempt.attributes["error.type"] == "RuntimeError" and attempt.attributes["ftw.attempt.final"] is True
    assert attempt.status.status_code == StatusCode.ERROR and not attempt.status.description
    assert "secret client text" not in json.dumps([dict(s.attributes) for s in spans_of(telemetry)], default=str)


async def test_a_rejected_output_is_an_error_on_the_attempt_that_check_output_rejected(db, telemetry):
    from fakes import Echo as Base

    class Picky(Base):
        def check_output(self, agent_input, output):
            if output.echoed == "no":
                raise ValueError("not this one")

    observer = observer_for(telemetry)
    runner = runner_with(db, Scripted(script=[echoed("no"), echoed("yes")]), observers=[observer])

    result = await runner.run(Picky, ECHO)

    assert result.status == RunStatus.SUCCESS and result.attempts == 2
    first, second = sorted(named(spans_of(telemetry), "invoke_agent echo"), key=lambda s: s.attributes["ftw.attempt"])
    assert first.attributes["error.type"] == "ValueError" and first.attributes["ftw.attempt.final"] is False
    assert second.attributes["ftw.attempt.final"] is True


# a reused step


async def test_a_reused_step_is_a_span_with_no_model_call_and_the_ids_to_find_the_original(db, telemetry):
    observer = observer_for(telemetry)
    runner = runner_with(db, model(), observers=[observer], max_attempts=1)
    first = await run_pipeline(db, "p", steps(runner, ["a", "b"]), resumable=True)
    assert len(spans_of(telemetry)) == 4  # two attempts, a model call each
    telemetry[1].clear()

    again = await run_pipeline(db, "p", steps(runner, ["a", "b"]), resume_from=first.job_id)

    spans = spans_of(telemetry)
    assert [s.name for s in spans] == ["invoke_agent echo", "invoke_agent echo"]  # and no chat span
    originals = {r.agent_run_id for r in await db.list_runs(job_id=first.job_id)}
    assert {s.attributes["ftw.reused_from_agent_run_id"] for s in spans} == originals
    assert all(s.attributes["ftw.reused"] is True and s.attributes["ftw.resumed_from"] == first.job_id for s in spans)
    assert all(s.attributes["ftw.job.id"] == again.job_id for s in spans) and "ftw.attempt" not in spans[0].attributes


# content


async def test_content_is_on_the_spans_only_when_asked(db, telemetry):
    observer = observer_for(telemetry)
    runner = runner_with(db, Scripted(script=[echoed()]), observers=[observer])

    await in_a_job(observer, lambda: runner.run(Echo, ECHO), input={"text": "hello"})

    assert not {key for s in spans_of(telemetry) for key in (s.attributes or {})} & CONTENT


async def test_content_follows_the_conventions_json_when_asked(db, telemetry):
    observer = observer_for(telemetry, capture_content=True)
    script = [launch_child("t-launch"), child_done(), parent_done()]
    runner = runner_with(db, Scripted(script=script), observers=[observer])

    await in_a_job(observer, lambda: runner.run(Parent, PARENT_INPUT), input={"task": "go"})

    spans = spans_of(telemetry)
    job = named(spans, "invoke_workflow pipe")[0].attributes
    assert json.loads(job["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": '{"task": "go"}'}]}
    ]
    parent = named(spans, "invoke_agent parent")[0].attributes
    assert json.loads(parent["gen_ai.system_instructions"])[0]["type"] == "text"
    assert json.loads(parent["gen_ai.output.messages"])[0]["role"] == "assistant"
    chat = [
        s
        for s in named(spans, "chat fake-model")
        if s.parent.span_id == named(spans, "invoke_agent parent")[0].context.span_id
    ]
    first_input = json.loads(chat[0].attributes["gen_ai.input.messages"])
    assert first_input[-1]["role"] == "user" and first_input[-1]["parts"][0]["type"] == "text"
    assert json.loads(chat[0].attributes["gen_ai.system_instructions"])[0]["type"] == "text"
    reply = json.loads(chat[0].attributes["gen_ai.output.messages"])[0]
    assert reply["role"] == "assistant" and any(p["type"] == "tool_call" for p in reply["parts"])
    tool = named(spans, "execute_tool launch_agent")[0].attributes
    assert tool["gen_ai.tool.call.id"] == "t-launch" and tool["gen_ai.tool.type"] == "function"
    assert json.loads(tool["gen_ai.tool.call.arguments"])["agent_name"] == "child"
    assert "SUCCESS" in tool["gen_ai.tool.call.result"]


def test_the_content_switch_follows_the_conventions_variable_and_an_argument_wins(monkeypatch):
    monkeypatch.delenv(CAPTURE_CONTENT_ENV, raising=False)
    assert OTelObserver().capture_content is False
    monkeypatch.setenv(CAPTURE_CONTENT_ENV, "span_only")
    assert OTelObserver().capture_content is True
    assert OTelObserver(capture_content=False).capture_content is False
    monkeypatch.setenv(CAPTURE_CONTENT_ENV, "EVENT_ONLY")  # not supported: no content
    assert OTelObserver().capture_content is False
    monkeypatch.setenv(CAPTURE_CONTENT_ENV, "NO_CONTENT")
    assert OTelObserver(capture_content=True).capture_content is True


# your own spans, and the trace across a queue


async def test_a_span_of_your_own_nests_between_the_job_and_the_attempts(db, telemetry):
    observer = observer_for(telemetry)
    runner = runner_with(db, Scripted(script=[echoed()]), observers=[observer])
    mine = telemetry[0].get_tracer("my app")

    async def pipeline():
        with mine.start_as_current_span("load documents"):
            return await runner.run(Echo, ECHO)

    await in_a_job(observer, pipeline)

    assert edges(spans_of(telemetry)) == [
        ("chat fake-model", "invoke_agent echo"),
        ("invoke_agent echo", "load documents"),
        ("invoke_workflow pipe", None),
        ("load documents", "invoke_workflow pipe"),
    ]


async def test_the_trace_context_travels_from_the_submitter_to_the_job(telemetry):
    client = observer_for(telemetry)
    worker = observer_for(telemetry)
    mine = telemetry[0].get_tracer("my api")

    with mine.start_as_current_span("POST /jobs") as request:
        carrier = await client.submitted(JobSubmitted(job_id="j1", name="pipe"))
    assert carrier is not None and "traceparent" in carrier

    async with worker.job(JobStart(job_id="j1", name="pipe", trace_context=carrier)) as scope:
        await scope.finish(JobEnd(status=RunStatus.SUCCESS))

    (job,) = named(spans_of(telemetry), "invoke_workflow pipe")
    assert job.context.trace_id == request.get_span_context().trace_id
    assert job.parent is not None and job.parent.span_id == request.get_span_context().span_id


async def test_a_job_without_a_carrier_starts_its_own_trace_under_the_current_span(telemetry):
    observer = observer_for(telemetry)

    async with observer.job(JobStart(job_id="j1", name="pipe")) as scope:
        await scope.finish(JobEnd(status=RunStatus.SUCCESS))

    (job,) = spans_of(telemetry)
    assert job.parent is None


async def test_a_failed_job_is_an_error_with_its_type(telemetry):
    observer = observer_for(telemetry)

    async with observer.job(JobStart(job_id="j1", name="pipe")) as scope:
        await scope.finish(JobEnd(status=RunStatus.FAILED, error_type="ValueError"))

    (job,) = spans_of(telemetry)
    assert job.status.status_code == StatusCode.ERROR and job.attributes["error.type"] == "ValueError"


async def test_a_cancelled_job_is_an_error_of_its_own_kind(telemetry):
    observer = observer_for(telemetry)

    async with observer.job(JobStart(job_id="j1", name="pipe")) as scope:
        await scope.finish(JobEnd(status=RunStatus.CANCELLED))

    (job,) = spans_of(telemetry)
    assert job.status.status_code == StatusCode.ERROR and job.attributes["error.type"] == "CancelledError"


# streaming, no SDK, and the kit


async def test_a_streaming_run_is_an_attempt_with_a_model_call(db, telemetry):
    use_model(Streams(turns=[["Hel", "lo"]]))
    runner = AgentRunner(db, observers=[observer_for(telemetry)])

    _ = [event async for event in runner.stream(Chat, say("hi"))]

    spans = spans_of(telemetry)
    assert [name for name, _parent in edges(spans)] == ["chat fake-model", "invoke_agent chat"]
    assert named(spans, "invoke_agent chat")[0].attributes["ftw.attempt.final"] is True


async def test_with_no_sdk_nothing_is_recorded_and_nothing_fails(db):
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)  # nothing here configures one
    observer = OTelObserver()
    runner = runner_with(db, Scripted(script=[echoed()]), observers=[observer])

    result = await in_a_job(observer, lambda: runner.run(Echo, ECHO))

    assert result.status == RunStatus.SUCCESS
    assert await observer.submitted(JobSubmitted(job_id="j1", name="pipe")) is None
    assert trace.get_current_span().get_span_context().is_valid is False


async def test_the_kit_accepts_the_observer(telemetry):
    await check_observer(lambda: observer_for(telemetry))
    await check_observer(lambda: observer_for(telemetry, capture_content=True))


# a job in a process, a job on a worker: the same spans


async def test_a_job_that_a_client_submits_and_a_worker_runs_is_one_trace_across_the_queue(db, telemetry):
    queue = SqliteQueue(db)
    client = JobClient(db, queue, MemoryResultStore(), observers=[observer_for(telemetry)])
    runner = runner_with(db, Scripted(script=[echoed()]), observers=[observer_for(telemetry)])
    worker = JobWorker(db, queue, runner, [Shout], results=MemoryResultStore(), receive_timeout=0.05, cancel_poll=0.05)
    api = telemetry[0].get_tracer("my api")

    with api.start_as_current_span("POST /jobs") as request:
        job_id = await client.submit(Shout, In(text="hello"))
    task = asyncio.create_task(worker.run())  # another task, another time: nothing of the request is current
    try:
        row = await wait_for_job(db, job_id, timeout=5)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(task, timeout=5)

    assert row.status == RunStatus.SUCCESS
    spans = spans_of(telemetry)
    assert edges(spans) == [
        ("POST /jobs", None),
        ("chat fake-model", "invoke_agent echo"),
        ("invoke_agent echo", "invoke_workflow shout"),
        ("invoke_workflow shout", "POST /jobs"),
    ]
    assert {s.context.trace_id for s in spans} == {request.get_span_context().trace_id}
    job = named(spans, "invoke_workflow shout")[0].attributes
    assert (job["ftw.job.id"], job["ftw.job.version"], job["ftw.job.hash"]) == (job_id, "3", Shout.job_hash())
    assert job["gen_ai.agent.name"] == "shout" and "ftw.resumed_from" not in job


async def test_a_job_run_in_your_process_has_the_same_spans_and_your_own_span_nests_in_it(db, telemetry):
    mine = telemetry[0].get_tracer("my app")

    class Mine(Job):
        name = "shout"
        version = "3"
        input_model = In
        output_model = Out

        async def run(self, ctx: JobContext, input: In) -> Out:
            with mine.start_as_current_span("load documents"):
                result = await ctx.run(Echo, EchoInput(text=input.text))
            return Out(shouted=result.output.echoed.upper())

    runner = runner_with(db, Scripted(script=[echoed()]), observers=[observer_for(telemetry)])

    outcome = await run_job(runner, Mine, In(text="hello"))

    assert outcome.status == RunStatus.SUCCESS
    assert edges(spans_of(telemetry)) == [
        ("chat fake-model", "invoke_agent echo"),
        ("invoke_agent echo", "load documents"),
        ("invoke_workflow shout", None),
        ("load documents", "invoke_workflow shout"),
    ]
    job = named(spans_of(telemetry), "invoke_workflow shout")[0]
    assert job.attributes["ftw.job.version"] == "3" and job.status.status_code == StatusCode.UNSET


async def test_a_job_that_fails_in_a_worker_or_in_your_process_is_an_error_span_without_its_text(db, telemetry):
    from test_job_observers import Boom

    runner = runner_with(db, Scripted(script=[]), observers=[observer_for(telemetry, capture_content=True)])

    outcome = await run_job(runner, Boom, In(text="x"))

    assert outcome.status == RunStatus.FAILED
    (job,) = named(spans_of(telemetry), "invoke_workflow boom")
    assert job.status.status_code == StatusCode.ERROR and job.attributes["error.type"] == "RuntimeError"
    assert "secret failure" not in json.dumps(dict(job.attributes), default=str)
