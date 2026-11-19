"""Default tools with a mocked LLM: store_get / store_set work."""

import json
from typing import ClassVar

from fakes import FakeSettings, Scripted, runner_with, structured_reply, tool_reply
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, RunStatus
from featherweight.store import RunStore
from featherweight.tools import store_get, store_set


class TaskInput(AgentInput):
    task: str


class TaskOutput(AgentOutput):
    result: str | None = None


class Notetaker(AgentBase):
    name: str = "notetaker"
    description: str = "Saves and reads notes"
    version: str = "0.1.0"
    prompt: str = "Save and read notes."
    model: ModelSettings = FakeSettings()
    tools: list[BaseTool] = [store_get, store_set]
    input_model: ClassVar[type[AgentInput]] = TaskInput
    output_model: ClassVar[type[AgentOutput]] = TaskOutput


def tool_outputs(model: Scripted) -> list[str]:
    """What the tools returned to the model, from the messages of its last call."""
    return [m.content for m in model.seen[-1] if isinstance(m, ToolMessage)]


def test_the_model_sees_only_its_own_arguments_never_the_run_context():
    assert set(store_get.args) == {"key"}
    assert set(store_set.args) == {"key", "value"}


# store tools


async def test_store_set_then_get_within_a_job(db):
    store = RunStore()
    model = Scripted(
        script=[
            tool_reply("store_set", {"key": "notes", "value": "remember this"}, "t1"),
            tool_reply("store_get", {"key": "notes"}, "t2"),
            structured_reply("TaskOutput", {"result": "done"}),
        ]
    )
    job_id = await db.create_job("learn")

    result = await runner_with(db, model, store=store).run(Notetaker, TaskInput(task="x"), job_id=job_id)

    assert result.status == RunStatus.SUCCESS
    assert await store.get(job_id, "notes") == "remember this"
    assert tool_outputs(model) == ["Saved 'notes'.", "remember this"]


async def test_store_is_shared_between_agents_of_the_same_job_only(db):
    store = RunStore()
    writer = Scripted(
        script=[tool_reply("store_set", {"key": "k", "value": "from job 1"}, "t1"), structured_reply("TaskOutput", {})]
    )
    reader_same_job = Scripted(script=[tool_reply("store_get", {"key": "k"}, "t2"), structured_reply("TaskOutput", {})])
    reader_other_job = Scripted(
        script=[tool_reply("store_get", {"key": "k"}, "t3"), structured_reply("TaskOutput", {})]
    )
    job1, job2 = await db.create_job("a"), await db.create_job("b")

    await runner_with(db, writer, store=store).run(Notetaker, TaskInput(task="x"), job_id=job1)
    await runner_with(db, reader_same_job, store=store).run(Notetaker, TaskInput(task="x"), job_id=job1)
    await runner_with(db, reader_other_job, store=store).run(Notetaker, TaskInput(task="x"), job_id=job2)

    assert tool_outputs(reader_same_job) == ["from job 1"]
    assert tool_outputs(reader_other_job) == ["Nothing is stored under 'k'."]


async def test_store_get_returns_json_text_for_structured_values(db):
    store = RunStore()
    job_id = await db.create_job("learn")
    await store.set(job_id, "parties", ["Anna", "Muster Ltd"])  # written by pipeline code, not the model
    model = Scripted(script=[tool_reply("store_get", {"key": "parties"}, "t1"), structured_reply("TaskOutput", {})])

    await runner_with(db, model, store=store).run(Notetaker, TaskInput(task="x"), job_id=job_id)

    assert json.loads(tool_outputs(model)[0]) == ["Anna", "Muster Ltd"]


async def test_store_tools_without_a_job_say_so_and_do_not_fail_the_run(db):
    model = Scripted(
        script=[
            tool_reply("store_set", {"key": "k", "value": "v"}, "t1"),
            tool_reply("store_get", {"key": "k"}, "t2"),
            structured_reply("TaskOutput", {}),
        ]
    )
    result = await runner_with(db, model).run(Notetaker, TaskInput(task="x"))

    assert result.status == RunStatus.SUCCESS
    assert all("no job" in out for out in tool_outputs(model))


async def test_runner_uses_its_own_store_by_default(db):
    model = Scripted(
        script=[tool_reply("store_set", {"key": "k", "value": "v"}, "t1"), structured_reply("TaskOutput", {})]
    )
    runner = runner_with(db, model)
    job_id = await db.create_job("learn")

    await runner.run(Notetaker, TaskInput(task="x"), job_id=job_id)

    assert await runner.store.get(job_id, "k") == "v"
