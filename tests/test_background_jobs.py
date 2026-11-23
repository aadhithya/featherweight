"""Background jobs: the declaration and its checks, the prompt, the tool, and the child as a job (here, and on a worker)."""

import asyncio
import json
from typing import ClassVar

import pytest
from fakes import FakeSettings, Reactive, Scripted, runner_with, structured_reply, tool_reply, use_model
from langchain_core.messages import SystemMessage, ToolMessage

from featherweight.background import BackgroundChildren
from featherweight.client import JobClient
from featherweight.context import IN_BACKGROUND_JOB
from featherweight.errors import SetupError
from featherweight.fingerprint import agent_declaration, agent_hash, input_hash, tools_hash
from featherweight.jobs import JobContext, job, run_job
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, RunStatus
from featherweight.queue import SqliteQueue
from featherweight.results import ResultKey
from featherweight.runner import AgentRunner
from featherweight.streaming import ChatInput, StreamingAgent
from featherweight.testing import MemoryQueue, MemoryResultStore
from featherweight.worker import JobWorker


class ChildInput(AgentInput):
    topic: str


class ChildOutput(AgentOutput):
    summary: str | None = None


class Child(AgentBase):
    name: str = "child"
    description: str = "Researches a topic"
    version: str = "0.1.0"
    prompt: str = "Research the topic."
    model: ModelSettings = FakeSettings(
        key="child"
    )  # the child has a model of its own, so that the two do not share a script
    input_model: ClassVar[type[AgentInput]] = ChildInput
    output_model: ClassVar[type[AgentOutput]] = ChildOutput


class ParentInput(AgentInput):
    task: str


class ParentOutput(AgentOutput):
    answer: str | None = None


class Parent(AgentBase):
    name: str = "parent"
    description: str = "Does a task, and starts a child in the background"
    version: str = "0.1.0"
    prompt: str = "Do the task."
    model: ModelSettings = FakeSettings()
    background_jobs: list[type] = [Child]
    input_model: ClassVar[type[AgentInput]] = ParentInput
    output_model: ClassVar[type[AgentOutput]] = ParentOutput


PARENT_INPUT = ParentInput(task="go")
CHILD_ARGS = {"agent_name": "child", "input_json": '{"topic": "x"}'}


def parent_done(answer="done"):
    return structured_reply("ParentOutput", {"answer": answer})


def child_done(summary="s"):
    return structured_reply("ChildOutput", {"summary": summary})


def start_child(call_id="t-start", **args):
    return tool_reply("start_background", {**CHILD_ARGS, **args}, call_id)


def tool_results(model) -> list[str]:
    """The text of every tool result the model was shown, in the order of the calls."""
    seen: list[str] = []
    for messages in model.seen:
        for message in messages:
            if (
                isinstance(message, ToolMessage)
                and message.content not in seen
                and not str(message.content).startswith("Returning structured response")  # the answer's own receipt
            ):
                seen.append(message.content)
    return seen


def prompts(model) -> set[str]:
    return {m.content for messages in model.seen for m in messages if isinstance(m, SystemMessage)}


@pytest.fixture
def results():
    return MemoryResultStore()


async def finished(runner: AgentRunner) -> None:
    """Wait for the children that run in this process."""
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)


# the declaration


def test_background_jobs_may_be_agent_classes_and_job_classes():
    @job
    async def summarise(ctx: JobContext, input: ChildInput) -> ChildOutput:
        return ChildOutput(summary="s")

    class Both(Parent):
        name: str = "both"
        background_jobs: list[type] = [Child, summarise]

    assert [getattr(e, "name", None) for e in Both().background_jobs if e is summarise] == ["summarise"]
    assert Both().background_entry("summarise") is summarise and Both().background_entry("child") is Child
    assert Both().background_entry("stranger") is None


def test_an_agent_may_be_a_subagent_and_a_background_agent_both():
    class Either(Parent):
        name: str = "either"
        subagents: list[type] = [Child]
        background_jobs: list[type] = [Child]

    assert Either().subagent_instance("child") is not None and Either().background_entry("child") is Child


def test_the_declaration_is_checked_when_the_agent_is_built():
    class Streamer(StreamingAgent):
        name: str = "streamer"
        description: str = "Streams"
        version: str = "1"
        prompt: str = "Talk."
        model: ModelSettings = FakeSettings()

    class Twin(Child):
        pass

    class Nested(Child):
        name: str = "nested"
        background_jobs: list[type] = [Child]

    class Itself(Parent):
        name: str = "itself"

    for entries in (
        [int],
        [Streamer],
        [Child, Twin],
        [Nested],
    ):  # not a class of ours, streaming, one name twice, a nested one
        with pytest.raises(ValueError):
            Parent(background_jobs=entries)
    with pytest.raises(ValueError, match="itself"):
        Itself(background_jobs=[Itself])


def test_a_job_class_cannot_be_a_subagent():
    @job
    async def summarise(ctx: JobContext, input: ChildInput) -> ChildOutput:
        return ChildOutput()

    with pytest.raises(ValueError, match="AgentBase"):
        Parent(subagents=[summarise])


def test_an_agent_without_background_jobs_keeps_its_hash_and_one_with_them_has_another():
    class Plain(Parent):
        background_jobs: list[type] = []

    assert agent_hash(Plain()) != agent_hash(Parent())
    assert "background_jobs" not in agent_declaration(Plain())


# the prompt and the tools


async def test_the_prompt_describes_each_agent_once_in_its_group(db):
    class Writer(Child):
        name: str = "writer"
        description: str = "Writes a draft"

    class Mixed(Parent):
        name: str = "mixed"
        subagents: list[type] = [Child, Writer]
        background_jobs: list[type] = [Child]

    model = Scripted(script=[parent_done()])
    await runner_with(db, model, results=MemoryResultStore()).run(Mixed, PARENT_INPUT)

    (prompt,) = prompts(model)
    assert prompt.count("- child:") == 1 and prompt.count("- writer:") == 1
    assert prompt.index("with `launch_agent`:") < prompt.index("- writer:") < prompt.index("Either way")
    assert prompt.index("Either way") < prompt.index("- child:")
    assert "start_background" in prompt and "launch_agent" in prompt


async def test_an_agent_with_only_subagents_has_the_prompt_it_always_had(db):
    class Inline(Parent):
        name: str = "inline"
        subagents: list[type] = [Child]
        background_jobs: list[type] = []

    model = Scripted(script=[parent_done()])
    await runner_with(db, model).run(Inline, PARENT_INPUT)

    (prompt,) = prompts(model)
    assert "You can launch other agents to do part of the work." in prompt and "start_background" not in prompt
    (row,) = await db.list_runs()
    assert row.tools_hash == tools_hash([], ["launch_agent", "get_agent_schema"])


async def test_the_tools_follow_the_declaration(db):
    class Both(Parent):
        name: str = "both"
        subagents: list[type] = [Child]

    model = Scripted(script=[parent_done()])
    await runner_with(db, model, results=MemoryResultStore()).run(Parent, PARENT_INPUT)
    (only_background,) = await db.list_runs()
    assert only_background.tools_hash == tools_hash([], ["start_background", "get_agent_schema"])

    model = Scripted(script=[parent_done()])
    await runner_with(db, model, results=MemoryResultStore()).run(Both, PARENT_INPUT)
    both = (await db.list_runs())[-1]
    assert both.tools_hash == tools_hash([], ["launch_agent", "get_agent_schema", "start_background"])


async def test_get_agent_schema_covers_the_background_jobs_too(db):
    @job
    async def summarise(ctx: JobContext, input: ChildInput) -> ChildOutput:
        """Summarises a page."""
        return ChildOutput()

    class Two(Parent):
        name: str = "two"
        background_jobs: list[type] = [Child, summarise]

    model = Scripted(
        script=[
            tool_reply("get_agent_schema", {"agent_name": "child"}, "t1"),
            tool_reply("get_agent_schema", {"agent_name": "summarise"}, "t2"),
            tool_reply("get_agent_schema", {"agent_name": "stranger"}, "t3"),
            parent_done(),
        ]
    )
    await runner_with(db, model, results=MemoryResultStore()).run(Two, PARENT_INPUT)

    unknown, child, summarise_schema = sorted(tool_results(model), key=lambda text: not text.startswith("'"))
    assert "You can use: child, summarise" in unknown
    assert json.loads(child)["description"] == "Researches a topic"
    assert json.loads(summarise_schema)["description"] == "Summarises a page."
    assert "topic" in json.loads(summarise_schema)["input_schema"]["properties"]


# the tool: every row of the table, here


async def test_start_background_returns_at_once_and_the_child_runs_as_a_job_of_its_own(db, results):
    use_model(Scripted(script=[child_done("found it")]), "child")
    model = Scripted(script=[start_child(), parent_done(), parent_done()])
    runner = runner_with(db, model, results=results)

    result = await runner.run(Parent, PARENT_INPUT)
    await finished(runner)

    assert result.status == RunStatus.SUCCESS
    started = json.loads(tool_results(model)[0])
    assert started["status"] == "STARTED" and started["agent"] == "child"
    child = await db.get_job(started["job_id"])
    assert child is not None and child.status == RunStatus.SUCCESS and child.name == "child"
    assert child.parent_agent_run_id == result.agent_run_id
    assert child.job_hash is not None and child.input_hash == input_hash(ChildInput(topic="x"))
    events = await db.list_job_events(started["job_id"])
    assert [e.type for e in events] == ["queued", "running", "succeeded"]
    kept = await results.get(ResultKey(job_id=child.job_id, agent_hash=child.job_hash, input_hash=child.input_hash))
    assert kept is not None and kept["summary"] == "found it"


async def test_the_runs_of_a_child_belong_to_its_job_and_not_to_the_parents(db, results):
    use_model(Scripted(script=[child_done()]), "child")
    model = Scripted(script=[start_child(), parent_done(), parent_done()])
    runner = runner_with(db, model, results=results)

    result = await runner.run(Parent, PARENT_INPUT)
    await finished(runner)

    started = json.loads(tool_results(model)[0])
    runs = await db.list_runs()
    child_runs = [r for r in runs if r.agent_name == "child"]
    assert len(child_runs) == 1 and child_runs[0].job_id == started["job_id"] and child_runs[0].depth == 1
    assert child_runs[0].parent_agent_run_id is None
    assert [r.agent_name for r in runs if r.job_id is None] == ["parent"] and result.status == RunStatus.SUCCESS


async def test_an_agent_that_is_not_in_the_list_is_refused_in_words(db, results):
    model = Scripted(script=[start_child(agent_name="stranger"), parent_done()])
    runner = runner_with(db, model, results=results)

    await runner.run(Parent, PARENT_INPUT)

    (text,) = tool_results(model)
    assert "'stranger' is not an agent you can start in the background. You can start: child." == text
    assert not runner._background_tasks


async def test_an_input_that_does_not_fit_is_refused_in_words_and_starts_nothing(db, results):
    model = Scripted(script=[start_child(input_json='{"wrong": 1}'), parent_done()])
    runner = runner_with(db, model, results=results)

    await runner.run(Parent, PARENT_INPUT)

    (text,) = tool_results(model)
    assert text.startswith("The input for 'child' is not valid") and "get_agent_schema" in text
    assert not runner._background_tasks


async def test_a_start_over_the_limit_is_refused_and_does_not_count(db, results):
    slow = Reactive(respond=lambda text, n: child_done(), delay_s=0.3)
    use_model(slow, "child")
    model = Scripted(script=[start_child("t1"), start_child("t2"), parent_done(), parent_done()])
    runner = runner_with(db, model, results=results, max_background=1)

    await runner.run(Parent, PARENT_INPUT)

    first, second = tool_results(model)
    assert json.loads(first)["status"] == "STARTED"
    assert "cannot start more agents in the background" in second and "1 are still running" in second
    assert len([r for r in await db.list_runs() if r.agent_name == "child"]) == 1  # the refused start made no job
    await finished(runner)
    children = BackgroundChildren(db, results)
    children.add(json.loads(first)["job_id"])
    assert await children.live() == 0  # a finished child does not count


async def test_a_job_class_can_be_started_in_the_background(db, results):
    @job
    async def summarise(ctx: JobContext, input: ChildInput) -> ChildOutput:
        """Summarises a page."""
        return ChildOutput(summary=input.topic.upper())

    class Jobby(Parent):
        name: str = "jobby"
        background_jobs: list[type] = [summarise]

    model = Scripted(
        script=[
            tool_reply("start_background", {"agent_name": "summarise", "input_json": '{"topic": "x"}'}, "t1"),
            parent_done(),
        ]
    )
    runner = runner_with(db, model, results=results)

    await runner.run(Jobby, PARENT_INPUT)
    await finished(runner)

    started = json.loads(tool_results(model)[0])
    child = await db.get_job(started["job_id"])
    assert child is not None and child.status == RunStatus.SUCCESS and child.job_version == "1"
    kept = await results.get(
        ResultKey(job_id=child.job_id, agent_hash=child.job_hash or "", input_hash=child.input_hash or "")
    )
    assert kept is not None and kept["summary"] == "X"


# a runner with no result store


async def test_an_agent_with_background_jobs_needs_a_result_store_before_any_model_call(db):
    model = Scripted(script=[parent_done()])
    runner = runner_with(db, model, results=None)

    with pytest.raises(SetupError, match="result store"):
        await runner.run(Parent, PARENT_INPUT)
    assert model.calls == 0


async def test_a_streaming_agent_with_background_jobs_is_refused_the_same_way(db):
    class Chat(StreamingAgent):
        name: str = "chat"
        description: str = "Talks to the user"
        version: str = "1"
        prompt: str = "You are a helpful chat agent."
        model: ModelSettings = FakeSettings()
        background_jobs: list[type] = [Child]

    runner = runner_with(db, Scripted(script=[]), results=None)
    with pytest.raises(SetupError, match="result store"):
        runner.stream(Chat, ChatInput.model_validate({"messages": [{"id": "m1", "role": "user", "content": "hi"}]}))


# a child inside a background job cannot start background jobs


async def test_what_a_background_job_runs_cannot_start_background_jobs(db):
    seen: list[bool] = []

    @job
    async def probe(ctx: JobContext, input: ChildInput) -> ChildOutput:
        seen.append(IN_BACKGROUND_JOB.get())
        return ChildOutput()

    runner = AgentRunner(db, results=MemoryResultStore())
    await run_job(runner, probe, ChildInput(topic="x"))
    background = await db.create_job("probe", parent_agent_run_id="some-attempt")
    await run_job(runner, probe, ChildInput(topic="x"), job_id=background)

    assert seen == [False, True] and IN_BACKGROUND_JOB.get() is False  # and nothing is left set afterwards
    assert AgentRunner._can_background(Parent()) is True
    token = IN_BACKGROUND_JOB.set(True)
    try:
        assert AgentRunner._can_background(Parent()) is False
    finally:
        IN_BACKGROUND_JOB.reset(token)


# on a worker: the same job, row, events and result


async def run_here(db, results):
    use_model(Scripted(script=[child_done("same")]), "child")
    model = Scripted(script=[start_child(), parent_done(), parent_done()])
    runner = runner_with(db, model, results=results)
    await runner.run(Parent, PARENT_INPUT)
    await finished(runner)
    return json.loads(tool_results(model)[0])["job_id"]


async def run_on_a_worker(db, results, queue):
    use_model(Scripted(script=[child_done("same")]), "child")
    client = JobClient(db, queue, results)
    worker_runner = AgentRunner(db, results=results)
    worker = JobWorker(db, queue, worker_runner, jobs=[Child], results=results, receive_timeout=0.05)
    running = asyncio.create_task(worker.run())
    try:
        model = Scripted(script=[start_child(), parent_done(), parent_done()])
        runner = runner_with(db, model, results=results, background=client)
        await runner.run(Parent, PARENT_INPUT)
        job_id = json.loads(tool_results(model)[0])["job_id"]
        await client.wait(job_id, timeout=10)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)
    return job_id


@pytest.mark.parametrize("make_queue", [MemoryQueue, None])
async def test_a_child_on_a_worker_is_the_same_job_as_a_child_here(db, make_queue):
    if make_queue is None:
        if not hasattr(db, "conn") or type(db).__name__ != "SqliteDatabase":
            pytest.skip("SqliteQueue needs the sqlite database")
        queue = SqliteQueue(db)
    else:
        queue = make_queue()
    here_results, worker_results = MemoryResultStore(), MemoryResultStore()

    here_id = await run_here(db, here_results)
    worker_id = await run_on_a_worker(db, worker_results, queue)

    here, there = await db.get_job(here_id), await db.get_job(worker_id)
    assert here is not None and there is not None
    for column in ("name", "status", "resumable", "job_version", "job_hash", "input_hash"):
        assert getattr(here, column) == getattr(there, column), column
    assert here.parent_agent_run_id is not None and there.parent_agent_run_id is not None
    assert [e.type for e in await db.list_job_events(here_id)] == [e.type for e in await db.list_job_events(worker_id)]
    key = ResultKey(job_id=here_id, agent_hash=here.job_hash or "", input_hash=here.input_hash or "")
    other = ResultKey(job_id=worker_id, agent_hash=there.job_hash or "", input_hash=there.input_hash or "")
    kept_here, kept_there = await here_results.get(key), await worker_results.get(other)
    assert kept_here is not None and kept_here == kept_there and kept_here["summary"] == "same"


async def test_a_worker_that_does_not_know_the_child_fails_its_job(db):
    queue = MemoryQueue()
    results = MemoryResultStore()
    client = JobClient(db, queue, results)
    worker = JobWorker(db, queue, AgentRunner(db, results=results), jobs=[], results=results, receive_timeout=0.05)
    running = asyncio.create_task(worker.run())
    try:
        model = Scripted(script=[start_child(), parent_done(), parent_done()])
        runner = runner_with(db, model, results=results, background=client)
        await runner.run(Parent, PARENT_INPUT)
        job_id = json.loads(tool_results(model)[0])["job_id"]
        row = await client.wait(job_id, timeout=10)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)
    assert row.status == RunStatus.FAILED
