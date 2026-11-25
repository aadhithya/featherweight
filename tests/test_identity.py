"""The identity of a run, in the process: the declared type, `identity=`, the tools that read it, who inherits it, what it never touches."""

import asyncio
import contextlib
import json
import logging
from typing import Any, ClassVar, Optional

import pytest
from ag_ui.core import UserMessage
from fakes import (
    FakeSettings,
    Reactive,
    Scripted,
    Streams,
    runner_with,
    structured_reply,
    tool_call_chunk,
    tool_reply,
    use_model,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, Field

from featherweight.context import CURRENT_IDENTITY, RunContext
from featherweight.fingerprint import agent_hash
from featherweight.identity import IdentitySpec
from featherweight.jobs import JobContext, job, run_job
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, Status
from featherweight.observers import Observer, Scope, ToolCallStart
from featherweight.otel import OTelObserver
from featherweight.runner import _Slot
from featherweight.streaming import ChatInput, StreamingAgent
from featherweight.testing import MemoryResultStore


class Teacher(BaseModel):
    user_id: str
    tenant: str = "uni"
    token: str = Field(default="", exclude=True)  # a value that exists only in this process


class Student(BaseModel):
    user_id: str


class Twin(BaseModel):  # the same fields as a Teacher: pydantic cannot tell their data apart
    user_id: str
    tenant: str = "uni"


SEEN: list[tuple[str, Any]] = []


@pytest.fixture(autouse=True)
def clean():
    SEEN.clear()


@tool
def whoami(runtime: ToolRuntime[RunContext[Teacher]]) -> str:
    """Say who is asking (a sync tool: it runs in a thread)."""
    SEEN.append(("sync", runtime.context.identity))
    return "ok"


@tool
async def whoami_async(runtime: ToolRuntime[RunContext[Teacher]]) -> str:
    """Say who is asking."""
    await asyncio.sleep(0)
    SEEN.append(("async", runtime.context.identity))
    return "ok"


@tool
async def whoami_shared() -> str:
    """Say who is asking, from the context variable: a shared tool cannot take `runtime`."""
    SEEN.append(("shared", CURRENT_IDENTITY.get()))
    return "ok"


class AskInput(AgentInput):
    text: str = ""


class AskOutput(AgentOutput):
    answer: str | None = None


class Asker(AgentBase):
    name: str = "asker"
    description: str = "Asks who is asking"
    version: str = "1"
    prompt: str = "Find out who is asking."
    model: ModelSettings = FakeSettings(key="asker")
    tools: list[BaseTool] = [whoami, whoami_async, whoami_shared]
    input_model: ClassVar[type[AgentInput]] = AskInput
    output_model: ClassVar[type[AgentOutput]] = AskOutput


def asks() -> Scripted:
    return Scripted(
        script=[
            tool_reply("whoami", {}, "t1"),
            tool_reply("whoami_async", {}, "t2"),
            tool_reply("whoami_shared", {}, "t3"),
            structured_reply("AskOutput", {"answer": "done"}),
        ]
    )


def kinds() -> list[str]:
    return [kind for kind, _ in SEEN]


# the declaration


@pytest.mark.parametrize("declared", [None, Teacher, Teacher | None, Optional[Teacher], Teacher | Student])  # noqa: UP045
def test_a_declaration_is_a_model_or_a_union_of_models_with_none(declared):
    IdentitySpec(declared)


@pytest.mark.parametrize("declared", [int, "Teacher", Teacher | int, type(None), list[Teacher], Teacher(user_id="a")])
def test_anything_else_is_refused_when_the_runner_is_built(declared, db):
    with pytest.raises(TypeError, match="identity_type"):
        runner_with(db, Scripted(script=[]), identity_type=declared)


@pytest.mark.parametrize(
    "declared,identity,ok",
    [
        (None, None, True),
        (None, Teacher(user_id="a"), False),
        (Teacher, Teacher(user_id="a"), True),
        (Teacher, None, False),
        (Teacher, Student(user_id="a"), False),
        (Teacher | None, None, True),
        (Teacher | None, Teacher(user_id="a"), True),
        (Teacher | None, Student(user_id="a"), False),
        (Teacher | Student, Student(user_id="a"), True),
    ],
    ids=[
        "nothing, none",
        "nothing, an identity",
        "required, given",
        "required, missing",
        "required, wrong type",
        "union, none",
        "union, given",
        "union, wrong type",
        "two models, the second",
    ],
)
def test_what_a_run_may_pass_follows_the_declaration(declared, identity, ok):
    if ok:
        IdentitySpec(declared).check(identity)
    else:
        with pytest.raises(TypeError, match="identity"):
            IdentitySpec(declared).check(identity)


def test_an_identity_is_dumped_without_its_excluded_fields_and_rebuilt_with_the_declared_type():
    spec = IdentitySpec(Teacher)
    data, kind = spec.dump(Teacher(user_id="a", token="secret"))
    assert data == {"user_id": "a", "tenant": "uni"}  # the token is not serialised
    assert kind == "Teacher"  # the name of the class
    assert spec.load(data, kind) == Teacher(user_id="a")
    assert IdentitySpec(Teacher | None).load(None) is None
    assert spec.dump(None) == (None, None)


@pytest.mark.parametrize("declared", [Teacher | Twin, Twin | Teacher, Teacher | Twin | None])
@pytest.mark.parametrize("sent", [Teacher(user_id="a"), Twin(user_id="a")], ids=["a teacher", "a twin"])
def test_a_union_of_models_with_the_same_fields_is_rebuilt_as_the_model_that_was_sent(declared, sent):
    spec = IdentitySpec(declared)
    rebuilt = spec.load(*spec.dump(sent))
    assert type(rebuilt) is type(sent)  # not the first model of the union that fits the data
    assert rebuilt == sent


def test_a_single_declared_type_refuses_the_data_of_another_model_with_the_same_fields():
    spec = IdentitySpec(Teacher)
    data, kind = spec.dump(Twin(user_id="a"))
    assert kind == "Twin"
    with pytest.raises(ValueError, match="not one of the declared types"):
        spec.load(data, kind)


def test_an_identity_without_a_kind_is_refused_and_so_is_a_kind_the_type_does_not_have():
    spec = IdentitySpec(Teacher | Student)
    with pytest.raises(ValueError, match="no kind"):
        spec.load({"user_id": "a"}, None)
    with pytest.raises(ValueError, match="not one of the declared types"):
        spec.load({"user_id": "a"}, "Principal")


def test_two_models_with_one_name_are_refused_when_the_declaration_is_made():
    def named(name: str) -> type[BaseModel]:
        return type(name, (BaseModel,), {"__annotations__": {"user_id": str}})

    with pytest.raises(TypeError, match="two models called 'Teacher'"):
        IdentitySpec(named("Teacher") | named("Teacher"))
    IdentitySpec(named("Teacher") | named("Student"))


class Principal(Teacher):  # a subclass is accepted wherever its parent is
    pass


@pytest.mark.parametrize(
    "runner,worker,ok",
    [
        (Teacher, Teacher, True),
        (Teacher | None, Teacher, True),  # a worker may be stricter than its runner
        (Teacher | Student, Teacher, True),
        (Teacher, Principal, True),
        (None, None, True),
        (Teacher | None, None, True),
        (Teacher | None, Teacher | None, True),
        (None, Teacher, False),  # the three mismatched rows: every job that runs an agent would fail
        (Teacher, Student, False),
        (Teacher, None, False),
        (Teacher, Teacher | None, False),  # a run without an identity, on a runner that requires one
        (Teacher, Teacher | Student, False),
        (Teacher | None, Student, False),
    ],
)
def test_a_runner_accepts_a_worker_whose_identities_are_all_ones_it_accepts(runner, worker, ok):
    assert IdentitySpec(runner).accepts(IdentitySpec(worker)) is ok


# the runner


async def test_a_run_without_a_declared_type_has_no_identity_and_refuses_one(db):
    use_model(asks(), "asker")
    runner = runner_with(db, Scripted(script=[]))
    result = await runner.run(Asker, AskInput())
    assert result.status == Status.SUCCESS
    assert [identity for _, identity in SEEN] == [None, None, None]
    with pytest.raises(TypeError, match="no identity_type is declared"):
        await runner.run(Asker, AskInput(), identity=Teacher(user_id="a"))


async def test_a_declared_type_makes_the_identity_required_and_checked(db):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    with pytest.raises(TypeError, match="every run needs identity="):
        await runner.run(Asker, AskInput())
    with pytest.raises(TypeError, match="identity must be a Teacher"):
        await runner.run(Asker, AskInput(), identity=Student(user_id="a"))
    with pytest.raises(TypeError, match="every run needs identity="):
        await runner.run_many(Asker, [AskInput()])
    assert await db.list_attempts() == []  # nothing ran, and nothing was written


async def test_a_union_with_none_allows_a_run_without_an_identity(db):
    use_model(asks(), "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher | None)
    teacher = Teacher(user_id="a")

    assert (await runner.run(Asker, AskInput())).status == Status.SUCCESS
    assert [identity for _, identity in SEEN] == [None, None, None]

    SEEN.clear()
    use_model(asks(), "asker")
    assert (await runner.run(Asker, AskInput(), identity=teacher)).status == Status.SUCCESS
    assert [identity for _, identity in SEEN] == [teacher, teacher, teacher]


# every entry point checks the identity, at the call, before anything is written


async def test_run_many_refuses_a_wrong_identity_for_one_input_and_for_several(db):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    for inputs in ([AskInput()], [AskInput(), AskInput(), AskInput()]):
        with pytest.raises(TypeError, match="identity must be a Teacher"):
            await runner.run_many(Asker, inputs, identity=Student(user_id="a"))
    assert await db.list_attempts() == []


async def test_run_many_refuses_an_identity_when_no_type_is_declared(db):
    runner = runner_with(db, Scripted(script=[]))
    with pytest.raises(TypeError, match="no identity_type is declared"):
        await runner.run_many(Asker, [AskInput()], identity=Teacher(user_id="a"))
    with pytest.raises(TypeError, match="no identity_type is declared"):
        await runner.run_many(Asker, [], identity=Teacher(user_id="a"))
    assert await db.list_attempts() == []


async def test_stream_refuses_a_wrong_identity_and_an_undeclared_one_at_the_call_not_when_the_loop_starts(db):
    declared = runner_with(db, Streams(turns=[["ok"]]), identity_type=Teacher)
    with pytest.raises(TypeError, match="identity must be a Teacher"):
        declared.stream(Chat, say("hi"), identity=Student(user_id="a"))
    undeclared = runner_with(db, Streams(turns=[["ok"]]))
    with pytest.raises(TypeError, match="no identity_type is declared"):
        undeclared.stream(Chat, say("hi"), identity=Teacher(user_id="a"))
    assert await db.list_attempts() == []


async def test_run_job_refuses_an_identity_when_no_type_is_declared(db):
    runner = runner_with(db, Scripted(script=[]))
    with pytest.raises(TypeError, match="no identity_type is declared"):
        await run_job(runner, ask_for, AskInput(), identity=Teacher(user_id="a"))
    assert await db.list_attempts() == []


# the tools


async def test_a_sync_tool_an_async_tool_and_a_shared_tool_all_see_the_identity_of_the_run(db):
    use_model(asks(), "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    teacher = Teacher(user_id="a", token="only-in-this-process")

    result = await runner.run(Asker, AskInput(), identity=teacher)

    assert result.status == Status.SUCCESS
    assert kinds() == ["sync", "async", "shared"]
    assert all(identity is teacher for _, identity in SEEN)  # the same object, in a process


async def test_two_runs_at_once_on_one_runner_do_not_mix_their_identities(db):
    def respond(user_text, call_number):
        if call_number == 1:
            return tool_reply("whoami_async", {}, f"t-{user_text}")
        return structured_reply("AskOutput", {"answer": "done"})

    use_model(Reactive(respond=respond), "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    first, second = Teacher(user_id="first"), Teacher(user_id="second")

    results = await asyncio.gather(
        runner.run(Asker, AskInput(text="one"), identity=first),
        runner.run(Asker, AskInput(text="two"), identity=second),
    )

    assert [r.status for r in results] == [Status.SUCCESS, Status.SUCCESS]
    assert sorted(identity.user_id for _, identity in SEEN) == ["first", "second"]
    assert CURRENT_IDENTITY.get() is None  # and the caller's own context is left as it was


async def test_run_many_gives_every_input_the_same_identity(db):
    def respond(user_text, call_number):
        if call_number == 1:
            return tool_reply("whoami_async", {}, f"t-{user_text}")
        return structured_reply("AskOutput", {"answer": "done"})

    use_model(Reactive(respond=respond), "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    teacher = Teacher(user_id="a")

    results = await runner.run_many(Asker, [AskInput(text=str(n)) for n in range(3)], identity=teacher)

    assert all(r.status == Status.SUCCESS for r in results)
    assert len(SEEN) == 3 and all(identity is teacher for _, identity in SEEN)


# who inherits it


class Boss(AgentBase):
    name: str = "boss"
    description: str = "Hands the question to a child"
    version: str = "1"
    prompt: str = "Ask the asker."
    model: ModelSettings = FakeSettings()
    subagents: list[type] = [Asker]
    input_model: ClassVar[type[AgentInput]] = AskInput
    output_model: ClassVar[type[AgentOutput]] = AskOutput


async def test_a_launched_attempts_with_the_identity_of_its_parent(db):
    parent = Scripted(
        script=[
            tool_reply("launch_agent", {"agent_name": "asker", "input_json": "{}"}, "t-launch"),
            structured_reply("AskOutput", {"answer": "done"}),
        ]
    )
    use_model(asks(), "asker")
    runner = runner_with(db, parent, identity_type=Teacher)
    teacher = Teacher(user_id="a")

    result = await runner.run(Boss, AskInput(), identity=teacher)

    assert result.status == Status.SUCCESS
    assert kinds() == ["sync", "async", "shared"]  # the child's tools
    assert all(identity is teacher for _, identity in SEEN)


class Starter(AgentBase):
    name: str = "starter"
    description: str = "Starts the asker in the background"
    version: str = "1"
    prompt: str = "Start the asker."
    model: ModelSettings = FakeSettings()
    background_jobs: list[type] = [Asker]
    input_model: ClassVar[type[AgentInput]] = AskInput
    output_model: ClassVar[type[AgentOutput]] = AskOutput


async def test_an_agent_started_in_the_background_here_runs_with_the_identity_of_its_parent(db):
    parent = Scripted(
        script=[
            tool_reply("start_background", {"agent_name": "asker", "input_json": "{}"}, "t-start"),
            structured_reply("AskOutput", {"answer": "done"}),
            structured_reply(
                "AskOutput", {"answer": "done"}
            ),  # a child that ends first comes back as a message: one more call
        ]
    )
    use_model(asks(), "asker")
    runner = runner_with(db, parent, identity_type=Teacher, results=MemoryResultStore())
    teacher = Teacher(user_id="a")

    result = await runner.run(Starter, AskInput(), identity=teacher)
    await asyncio.gather(*runner._background_tasks)

    assert result.status == Status.SUCCESS, result.error
    assert kinds() == ["sync", "async", "shared"]
    assert all(identity is teacher for _, identity in SEEN)


# a job


@job
async def ask_for(ctx: JobContext, input: AskInput) -> AskOutput:
    result = await ctx.run(Asker, input)
    return AskOutput(answer=f"{ctx.identity.user_id}:{result.status.value}" if ctx.identity else "nobody")


async def test_a_job_has_the_identity_it_was_run_for_and_so_does_every_agent_it_runs(db):
    use_model(asks(), "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    teacher = Teacher(user_id="a")

    outcome = await run_job(runner, ask_for, AskInput(), identity=teacher)

    assert outcome.status == Status.SUCCESS and outcome.result.answer == "a:SUCCESS"
    assert all(identity is teacher for _, identity in SEEN)


async def test_run_job_checks_the_identity_before_it_creates_a_job(db):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    with pytest.raises(TypeError, match="every run needs identity="):
        await run_job(runner, ask_for, AskInput())
    with pytest.raises(TypeError, match="identity must be a Teacher"):
        await run_job(runner, ask_for, AskInput(), identity=Student(user_id="a"))


# a streamed run


class Chat(StreamingAgent):
    name: str = "chat"
    description: str = "Talks"
    version: str = "1"
    prompt: str = "Chat."
    model: ModelSettings = FakeSettings()
    tools: list[BaseTool] = [whoami_async]


def say(text: str) -> ChatInput:
    return ChatInput(messages=[UserMessage(id="m1", content=text)], thread_id="t-1")


async def test_a_streamed_run_has_the_identity_too_and_checks_it_when_it_is_asked_for(db):
    model = Streams(turns=[[tool_call_chunk("whoami_async", {}, "c1")], ["done"]])
    runner = runner_with(db, model, identity_type=Teacher)
    with pytest.raises(TypeError, match="every run needs identity="):
        runner.stream(Chat, say("hi"))  # at the call, not when the loop starts
    teacher = Teacher(user_id="a")

    events = [event async for event in runner.stream(Chat, say("hi"), identity=teacher)]

    assert events[-1].type.value == "RUN_FINISHED"
    assert [kind for kind, _ in SEEN] == ["async"] and SEEN[0][1] is teacher


# a job is for one identity


async def run_in(runner, db, job_id, identity, *, many=False):
    use_model(asks(), "asker")
    if many:
        return await runner.run_many(Asker, [AskInput()], job_id=job_id, identity=identity)
    return await runner.run(Asker, AskInput(), job_id=job_id, identity=identity)


async def test_a_run_for_another_identity_is_refused_before_anything_is_written(db):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher | None)
    job_id = await db.create_job("pipeline")
    await run_in(runner, db, job_id, Teacher(user_id="u-distinct-1"))
    written, seen = len(await db.list_attempts()), len(SEEN)

    with pytest.raises(ValueError, match="a job is for one identity") as raised:
        await run_in(runner, db, job_id, Teacher(user_id="u-distinct-2"))

    assert "u-distinct" not in str(raised.value)  # the text names no identity value
    assert len(await db.list_attempts()) == written and len(SEEN) == seen  # nothing was written, and no tool ran


async def test_the_same_identity_twice_in_one_job_is_fine(db):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    job_id = await db.create_job("pipeline")
    for _ in range(2):
        assert (await run_in(runner, db, job_id, Teacher(user_id="a"))).status == Status.SUCCESS


@pytest.mark.parametrize(
    "first,second",
    [
        (Teacher(user_id="a"), None),
        (None, Teacher(user_id="a")),
        (Teacher(user_id="a"), Twin(user_id="a")),  # the same data, and another kind of identity
    ],
    ids=["an identity, then none", "none, then an identity", "two kinds with the same data"],
)
async def test_an_identity_none_and_a_kind_of_identity_are_each_another_identity(db, first, second):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher | Twin | None)
    job_id = await db.create_job("pipeline")
    await run_in(runner, db, job_id, first)

    with pytest.raises(ValueError, match="a job is for one identity"):
        await run_in(runner, db, job_id, second)


async def test_runs_with_no_job_are_not_checked(db):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    for user in ("a", "b"):
        assert (await run_in(runner, db, None, Teacher(user_id=user))).status == Status.SUCCESS


async def test_run_many_checks_the_identity_and_the_job_once_at_the_call_also_for_no_inputs(db, caplog):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    job_id = await db.create_job("pipeline")
    await run_in(runner, db, job_id, Teacher(user_id="a"))

    with caplog.at_level(logging.INFO):
        with pytest.raises(TypeError, match="every run needs identity="):
            await runner.run_many(Asker, [])
        with pytest.raises(TypeError, match="identity must be a Teacher"):
            await runner.run_many(Asker, [AskInput(), AskInput()], identity=Student(user_id="a"))
        with pytest.raises(ValueError, match="a job is for one identity"):
            await runner.run_many(Asker, [], job_id=job_id, identity=Teacher(user_id="b"))
        with pytest.raises(ValueError, match="a job is for one identity"):
            await runner.run_many(Asker, [AskInput()], job_id=job_id, identity=Teacher(user_id="b"))

    assert "fan_out_started" not in caplog.text  # refused before the first log line
    assert await runner.run_many(Asker, [], identity=Teacher(user_id="a")) == []  # a good call with no inputs is fine


async def test_a_streamed_run_for_another_identity_is_refused_when_the_loop_starts_and_writes_nothing(db):
    runner = runner_with(db, Streams(turns=[["ok"]]), identity_type=Teacher)
    job_id = await db.create_job("chat")
    await run_in(runner, db, job_id, Teacher(user_id="a"))
    written = len(await db.list_attempts())

    run = runner.stream(Chat, say("hi"), job_id=job_id, identity=Teacher(user_id="b"))  # nothing is raised at the call
    with pytest.raises(ValueError, match="a job is for one identity"):
        _ = [event async for event in run]

    assert len(await db.list_attempts()) == written


# one source for the identity of a background child


class Both(AgentBase):
    name: str = "both"
    description: str = "Launches and starts"
    version: str = "1"
    prompt: str = "Launch or start."
    model: ModelSettings = FakeSettings()
    subagents: list[type] = [Asker]
    background_jobs: list[type] = [Asker]
    input_model: ClassVar[type[AgentInput]] = AskInput
    output_model: ClassVar[type[AgentOutput]] = AskOutput


async def test_the_tools_the_model_sees_for_launching_have_no_identity_to_set(db):
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher, results=MemoryResultStore())
    context = RunContext(job_id=None, run_id="r", attempt_id="a", identity=Teacher(user_id="a"))
    tools = {t.name: t for t in runner._launch_tools(Both(), context, _Slot(asyncio.Semaphore(1)), depth=1)}

    assert set(tools) == {"launch_agent", "start_background", "get_agent_schema"}
    # the model fills these and nothing else: "the model cannot set, change or drop it"
    assert set(tools["launch_agent"].args) == {"agent_name", "input_json"}
    assert set(tools["start_background"].args) == {"agent_name", "input_json"}
    assert set(tools["get_agent_schema"].args) == {"agent_name"}


class Recorder(Observer):
    """Keeps every record that the runner hands to an observer, content included."""

    capture_content = True

    def __init__(self):
        self.records: list[BaseModel] = []

    def job(self, start):
        self.records.append(start)
        return super().job(start)

    def attempt(self, start):
        self.records.append(start)
        return self._scope()

    @contextlib.asynccontextmanager
    async def _scope(self):
        recorder = self

        class Keeping(Scope):
            async def finish(self, end):
                recorder.records.append(end)

        yield Keeping()

    async def reused(self, run):
        self.records.append(run)

    async def model_call_started(self, call):
        self.records.append(call)

    async def model_call_finished(self, call):
        self.records.append(call)

    async def tool_call_started(self, call):
        self.records.append(call)

    async def tool_call_finished(self, call):
        self.records.append(call)


def an_observed_runner(db, model=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    recorder = Recorder()
    runner = runner_with(
        db,
        model or Scripted(script=[]),
        identity_type=Teacher,
        observers=[OTelObserver(capture_content=True, tracer_provider=provider), recorder],
    )
    return runner, recorder, exporter


def the_identity_is_in(recorder: Recorder, exporter: InMemorySpanExporter) -> list[str]:
    """Where, among the spans' attributes and the observers' records, one of the identity's distinctive values is."""
    values = ("u-distinct-123", "t-distinct-456")
    found = []
    for span in exporter.get_finished_spans():
        text = json.dumps(dict(span.attributes or {}), default=str)
        found += [f"span {span.name}: {v}" for v in values if v in text]
    for record in recorder.records:
        text = record.model_dump_json()
        found += [f"{type(record).__name__}: {v}" for v in values if v in text]
    return found


def test_the_search_for_the_identity_in_what_an_observer_hears_finds_it_where_it_is():
    recorder, exporter = Recorder(), InMemorySpanExporter()
    assert the_identity_is_in(recorder, exporter) == []
    recorder.records.append(
        ToolCallStart(call_id="c", attempt_id="a", tool_name="t", arguments={"who": "u-distinct-123"})
    )
    assert the_identity_is_in(recorder, exporter) == ["ToolCallStart: u-distinct-123"]


async def test_an_observer_and_a_span_never_see_the_identity(db):
    use_model(asks(), "asker")
    runner, recorder, exporter = an_observed_runner(db)

    result = await runner.run(Asker, AskInput(), identity=Teacher(user_id="u-distinct-123", tenant="t-distinct-456"))

    assert result.status == Status.SUCCESS and kinds() == ["sync", "async", "shared"]  # the tools did read it
    assert {type(r).__name__ for r in recorder.records} >= {
        "AttemptStart",
        "ToolCallStart",
        "ToolCallEnd",
        "AttemptEnd",
    }
    assert exporter.get_finished_spans()
    assert the_identity_is_in(recorder, exporter) == []


async def test_a_streamed_run_shows_no_identity_to_an_observer_or_a_span_either(db):
    model = Streams(turns=[[tool_call_chunk("whoami_async", {}, "c1")], ["done"]])
    runner, recorder, exporter = an_observed_runner(db, model)

    events = [
        e
        async for e in runner.stream(
            Chat, say("hi"), identity=Teacher(user_id="u-distinct-123", tenant="t-distinct-456")
        )
    ]

    assert events[-1].type.value == "RUN_FINISHED" and [kind for kind, _ in SEEN] == ["async"]
    assert any(type(r).__name__ == "ToolCallStart" for r in recorder.records)
    assert the_identity_is_in(recorder, exporter) == []


# what it never touches


async def test_an_identity_is_not_in_the_hash_the_prompt_the_messages_the_log_or_the_run_log(db, caplog):
    assert agent_hash(Asker()) == agent_hash(Asker())  # an agent is not changed by who runs it
    model = asks()
    use_model(model, "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    teacher = Teacher(user_id="u-distinct-123", tenant="t-distinct-456")

    with caplog.at_level(logging.DEBUG):
        result = await runner.run(Asker, AskInput(), identity=teacher)

    assert result.status == Status.SUCCESS
    for text in ("u-distinct-123", "t-distinct-456"):
        assert text not in repr(model.seen)  # not in anything the model saw
        assert text not in caplog.text
        assert text not in repr(await db.list_attempts())
        assert text not in repr(result)
