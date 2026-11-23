"""The identity of a run, in the process: the declared type, `identity=`, the tools that read it, who inherits it, what it never touches."""

import asyncio
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
from pydantic import BaseModel, Field

from featherweight.context import CURRENT_IDENTITY, RunContext
from featherweight.fingerprint import agent_hash
from featherweight.identity import IdentitySpec
from featherweight.jobs import JobContext, job, run_job
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, RunStatus
from featherweight.streaming import ChatInput, StreamingAgent
from featherweight.testing import MemoryResultStore


class Teacher(BaseModel):
    user_id: str
    tenant: str = "uni"
    token: str = Field(default="", exclude=True)  # a value that exists only in this process


class Student(BaseModel):
    user_id: str


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
    data = spec.dump(Teacher(user_id="a", token="secret"))
    assert data == {"user_id": "a", "tenant": "uni"}  # the token is not serialised
    assert spec.load(data) == Teacher(user_id="a")
    assert IdentitySpec(Teacher | None).load(None) is None


# the runner


async def test_a_run_without_a_declared_type_has_no_identity_and_refuses_one(db):
    use_model(asks(), "asker")
    runner = runner_with(db, Scripted(script=[]))
    result = await runner.run(Asker, AskInput())
    assert result.status == RunStatus.SUCCESS
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
    assert await db.list_runs() == []  # nothing ran, and nothing was written


async def test_a_union_with_none_allows_a_run_without_an_identity(db):
    use_model(asks(), "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher | None)
    teacher = Teacher(user_id="a")

    assert (await runner.run(Asker, AskInput())).status == RunStatus.SUCCESS
    assert [identity for _, identity in SEEN] == [None, None, None]

    SEEN.clear()
    use_model(asks(), "asker")
    assert (await runner.run(Asker, AskInput(), identity=teacher)).status == RunStatus.SUCCESS
    assert [identity for _, identity in SEEN] == [teacher, teacher, teacher]


# the tools


async def test_a_sync_tool_an_async_tool_and_a_shared_tool_all_see_the_identity_of_the_run(db):
    use_model(asks(), "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    teacher = Teacher(user_id="a", token="only-in-this-process")

    result = await runner.run(Asker, AskInput(), identity=teacher)

    assert result.status == RunStatus.SUCCESS
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

    assert [r.status for r in results] == [RunStatus.SUCCESS, RunStatus.SUCCESS]
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

    assert all(r.status == RunStatus.SUCCESS for r in results)
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


async def test_a_launched_agent_runs_with_the_identity_of_its_parent(db):
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

    assert result.status == RunStatus.SUCCESS
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

    assert result.status == RunStatus.SUCCESS, result.error
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

    assert outcome.status == RunStatus.SUCCESS and outcome.result.answer == "a:SUCCESS"
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


# what it never touches


async def test_an_identity_is_not_in_the_hash_the_prompt_the_messages_the_log_or_the_run_log(db, caplog):
    assert agent_hash(Asker()) == agent_hash(Asker())  # an agent is not changed by who runs it
    model = asks()
    use_model(model, "asker")
    runner = runner_with(db, Scripted(script=[]), identity_type=Teacher)
    teacher = Teacher(user_id="u-distinct-123", tenant="t-distinct-456")

    with caplog.at_level(logging.DEBUG):
        result = await runner.run(Asker, AskInput(), identity=teacher)

    assert result.status == RunStatus.SUCCESS
    for text in ("u-distinct-123", "t-distinct-456"):
        assert text not in repr(model.seen)  # not in anything the model saw
        assert text not in caplog.text
        assert text not in repr(await db.list_runs())
        assert text not in repr(result)
