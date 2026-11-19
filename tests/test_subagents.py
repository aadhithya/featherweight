"""Agents that launch agents: the allow-list, the two tools, the depth and breadth limits, and the run log."""

import asyncio
import json
from typing import ClassVar

import pytest
from fakes import FakeSettings, Reactive, Scripted, runner_with, structured_reply, tool_reply
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from micro.context import RunContext
from micro.model_settings import ModelSettings
from micro.models import AgentBase, AgentInput, AgentOutput, RunStatus
from micro.runner import _Slot
from micro.store import RunStore


class ChildInput(AgentInput):
    topic: str


class ChildOutput(AgentOutput):
    summary: str | None = None


class Child(AgentBase):
    name: str = "child"
    description: str = "Summarises a topic"
    version: str = "0.1.0"
    prompt: str = "Summarise the topic."
    model: ModelSettings = FakeSettings()
    input_model: ClassVar[type[AgentInput]] = ChildInput
    output_model: ClassVar[type[AgentOutput]] = ChildOutput


class ParentInput(AgentInput):
    task: str


class ParentOutput(AgentOutput):
    answer: str | None = None


class Parent(AgentBase):
    name: str = "parent"
    description: str = "Does a task with the help of a child"
    version: str = "0.1.0"
    prompt: str = "Do the task."
    model: ModelSettings = FakeSettings()
    subagents: list[type] = [Child]
    input_model: ClassVar[type[AgentInput]] = ParentInput
    output_model: ClassVar[type[AgentOutput]] = ParentOutput


class Leaf(Child):
    name: str = "leaf"
    description: str = "The end of the chain"


class Mid(Parent):
    name: str = "mid"
    description: str = "A middle agent"
    subagents: list[type] = [Leaf]


class Top(Parent):
    name: str = "top"
    subagents: list[type] = [Mid]


PARENT_INPUT = ParentInput(task="go")


def launch_child(call_id="t-launch"):
    """A new message each time: the same message object twice in one script would be merged into one."""
    return tool_reply("launch_agent", {"agent_name": "child", "input_json": '{"topic": "x"}'}, call_id)


def parent_done(answer="done", **kwargs):
    return structured_reply("ParentOutput", {"answer": answer}, **kwargs)


def child_done(summary="s", **kwargs):
    return structured_reply("ChildOutput", {"summary": summary}, **kwargs)


def tool_messages(model) -> list[str]:
    """The text of every tool result the model was shown, over all its calls."""
    seen = {m.content for messages in model.seen for m in messages if isinstance(m, ToolMessage)}
    return sorted(seen)


def system_prompts(model) -> set[str]:
    return {m.content for messages in model.seen for m in messages if isinstance(m, SystemMessage)}


# the allow-list in the prompt, and the tools


async def test_the_prompt_lists_the_allowed_agents_and_the_run_log_the_launch_tools(db):
    model = Scripted(script=[parent_done()])

    result = await runner_with(db, model).run(Parent, PARENT_INPUT)

    assert result.status == RunStatus.SUCCESS
    (prompt,) = system_prompts(model)
    assert "- child: Summarises a topic" in prompt and "get_agent_schema" in prompt and "launch_agent" in prompt
    assert "input schema" not in prompt.lower().replace(
        "input it expects", ""
    )  # schemas come from the tool, not the prompt
    (row,) = await db.list_runs()
    assert row.tool_names == ["launch_agent", "get_agent_schema"] and row.depth == 1 and row.parent_agent_run_id is None


async def test_an_agent_without_subagents_gets_no_launch_tools_and_no_prompt_section(db):
    model = Scripted(script=[child_done()])

    await runner_with(db, model).run(Child, ChildInput(topic="x"))

    (prompt,) = system_prompts(model)
    assert "launch_agent" not in prompt
    assert (await db.list_runs())[0].tool_names == []


async def test_get_agent_schema_returns_the_input_and_output_schema_on_demand(db):
    model = Scripted(
        script=[
            tool_reply("get_agent_schema", {"agent_name": "child"}, "t1"),
            tool_reply("get_agent_schema", {"agent_name": "stranger"}, "t2"),
            parent_done(),
        ]
    )

    await runner_with(db, model).run(Parent, PARENT_INPUT)

    unknown, known = tool_messages(model)  # sorted: the plain-text refusal comes before the JSON
    known = json.loads(known)
    assert known["agent"] == "child" and known["description"] == "Summarises a topic"
    assert "topic" in known["input_schema"]["properties"] and "summary" in known["output_schema"]["properties"]
    assert "not an agent you can launch" in unknown and "child" in unknown


# launching


async def test_a_launch_runs_the_child_in_the_same_job_and_returns_its_output_to_the_parent(db):
    model = Scripted(script=[launch_child(), child_done("the summary"), parent_done()])
    job_id = await db.create_job("launch")

    result = await runner_with(db, model).run(Parent, PARENT_INPUT, job_id=job_id)

    assert result.status == RunStatus.SUCCESS and result.output.answer == "done"
    parent_row, child_row = await db.list_runs(job_id=job_id)
    assert (parent_row.agent_name, parent_row.depth, parent_row.parent_agent_run_id) == ("parent", 1, None)
    assert (child_row.agent_name, child_row.depth) == ("child", 2)
    assert child_row.parent_agent_run_id == parent_row.agent_run_id and child_row.input == {"topic": "x"}
    (returned,) = (json.loads(t) for t in tool_messages(model))
    assert returned == {
        "status": "SUCCESS",
        "agent": "child",
        "output": {"open_questions": [], "summary": "the summary"},
    }


async def test_the_parents_tokens_are_its_own_and_the_child_has_its_own_row(db):
    model = Scripted(
        script=[
            tool_reply(
                "launch_agent",
                {"agent_name": "child", "input_json": '{"topic": "x"}'},
                "t",
                input_tokens=3,
                output_tokens=2,
            ),
            child_done(input_tokens=11, output_tokens=13),
            parent_done(input_tokens=40, output_tokens=7),  # its input includes the child's output as a tool result
        ]
    )

    result = await runner_with(db, model).run(Parent, PARENT_INPUT)

    assert (result.input_tokens, result.output_tokens) == (43, 9)
    parent_row, child_row = await db.list_runs()
    assert (child_row.input_tokens, child_row.output_tokens) == (11, 13)
    assert parent_row.input_tokens == 43


@pytest.mark.parametrize(
    "call,expected",
    [
        ({"agent_name": "stranger", "input_json": "{}"}, "not an agent you can launch"),
        ({"agent_name": "child", "input_json": '{"nope": 1}'}, "is not valid"),
        ({"agent_name": "child", "input_json": "not json"}, "is not valid"),
    ],
    ids=["not on the allow-list", "wrong fields", "not json"],
)
async def test_a_refused_launch_is_told_to_the_parent_which_carries_on(db, call, expected):
    model = Scripted(script=[tool_reply("launch_agent", call, "t"), parent_done("carried on")])

    result = await runner_with(db, model).run(Parent, PARENT_INPUT)

    assert result.status == RunStatus.SUCCESS and result.output.answer == "carried on"
    assert [r.agent_name for r in await db.list_runs()] == ["parent"]  # nothing was launched
    (text,) = tool_messages(model)
    assert expected in text


async def test_a_child_that_fails_comes_back_as_a_failed_result_and_the_parent_carries_on(db):
    model = Scripted(
        script=[launch_child(), RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom"), parent_done()]
    )

    result = await runner_with(db, model).run(Parent, PARENT_INPUT)

    assert result.status == RunStatus.SUCCESS
    parent_row, *children = await db.list_runs()
    assert [c.status for c in children] == [RunStatus.FAILED] * 3  # the child's own retries
    assert all(c.parent_agent_run_id == parent_row.agent_run_id for c in children)
    (returned,) = (json.loads(t) for t in tool_messages(model))
    assert returned["status"] == "FAILED" and "boom" in returned["error"]


# the limits


async def test_depth_stops_the_chain_where_the_limit_is(db):
    model = Scripted(
        script=[
            tool_reply("launch_agent", {"agent_name": "mid", "input_json": '{"task": "m"}'}, "t"),
            parent_done("from mid"),
            parent_done("from top"),
        ]
    )

    result = await runner_with(db, model, max_depth=2).run(Top, PARENT_INPUT)

    assert result.status == RunStatus.SUCCESS
    top_row, mid_row = await db.list_runs()
    assert (top_row.depth, mid_row.depth) == (1, 2)
    assert top_row.tool_names == ["launch_agent", "get_agent_schema"]
    assert mid_row.tool_names == []  # Mid has a sub-agent, but no depth left to launch it
    mid_prompt = next(
        p for p in system_prompts(model) if "Do the task." in p and "- leaf" not in p and "- mid" not in p
    )
    assert "launch_agent" not in mid_prompt


async def test_a_chain_can_go_as_deep_as_the_limit_allows(db):
    model = Scripted(
        script=[
            tool_reply("launch_agent", {"agent_name": "mid", "input_json": '{"task": "m"}'}, "t1"),
            tool_reply("launch_agent", {"agent_name": "leaf", "input_json": '{"topic": "l"}'}, "t2"),
            child_done("leaf done"),
            parent_done("mid done"),
            parent_done("top done"),
        ]
    )

    result = await runner_with(db, model, max_depth=3).run(Top, PARENT_INPUT)

    assert result.output.answer == "top done"
    rows = await db.list_runs()
    assert [(r.agent_name, r.depth) for r in rows] == [("top", 1), ("mid", 2), ("leaf", 3)]
    assert rows[2].parent_agent_run_id == rows[1].agent_run_id and rows[1].parent_agent_run_id == rows[0].agent_run_id


async def test_a_depth_of_one_turns_launching_off(db):
    model = Scripted(script=[parent_done()])

    await runner_with(db, model, max_depth=1).run(Parent, PARENT_INPUT)

    assert (await db.list_runs())[0].tool_names == []
    assert all("launch_agent" not in p for p in system_prompts(model))


async def test_the_launch_tool_refuses_at_the_depth_limit_even_if_it_is_called(db):
    runner = runner_with(db, Scripted(script=[]), max_depth=2)
    context = RunContext(store=RunStore(), job_id=None, agent_run_id="a")

    launch, _ = runner._launch_tools(Mid(), context, _Slot(asyncio.Semaphore(1)), depth=2)
    text = await launch.ainvoke({"agent_name": "leaf", "input_json": '{"topic": "x"}'})

    assert "limit of 2 nested agents" in text
    assert await db.list_runs() == []


async def test_one_attempt_cannot_launch_more_than_the_breadth_limit(db):
    model = Scripted(script=[launch_child("t1"), child_done(), launch_child("t2"), parent_done()])

    result = await runner_with(db, model, max_launches=1).run(Parent, PARENT_INPUT)

    assert result.status == RunStatus.SUCCESS
    assert [r.agent_name for r in await db.list_runs()] == ["parent", "child"]  # the second launch did not happen
    assert any("limit is 1 launches" in t for t in tool_messages(model))


# the agent definition


def test_an_agent_cannot_launch_its_own_class():
    with pytest.raises(ValueError, match="can only launch other agents"):
        Parent(subagents=[Parent])


def test_subagents_must_be_agent_classes_with_distinct_names():
    with pytest.raises(ValueError, match="must be AgentBase classes"):
        Parent(subagents=[dict])
    with pytest.raises(ValueError, match="duplicate names"):
        Parent(subagents=[Child, Child])


async def test_a_bad_subagents_list_is_a_programming_error_that_raises(db):
    class Bad(Parent):
        subagents: list[type] = [dict]

    with pytest.raises(ValueError, match="must be AgentBase classes"):
        await runner_with(db, Scripted(script=[])).run(Bad, PARENT_INPUT)


def test_the_prompt_hash_differs_with_and_without_the_launch_section():
    assert Parent().build_system_prompt() != Parent().build_system_prompt(can_launch=False)
    assert Parent().build_system_prompt(can_launch=False) == Child(prompt=Parent().prompt).build_system_prompt()


# configuration


# concurrency: a parent gives its slot to the child


def reactive_respond(user_text, call_number):
    payload = json.loads(user_text)
    if "topic" in payload:
        return child_done(payload["topic"])
    if call_number == 1:
        return tool_reply(
            "launch_agent", {"agent_name": "child", "input_json": json.dumps({"topic": payload["task"]})}, "t"
        )
    return parent_done(payload["task"])


async def test_a_parent_waiting_for_its_child_does_not_hold_the_only_slot(db):
    model = Reactive(respond=reactive_respond)
    runner = runner_with(db, model, max_concurrent=1)

    result = await asyncio.wait_for(runner.run(Parent, ParentInput(task="a")), timeout=10)  # a deadlock would time out

    assert result.status == RunStatus.SUCCESS
    assert model.max_in_flight == 1  # still one model call at a time: the limit holds
    assert runner._slots._value == 1  # and the slot is back


async def test_many_parents_each_launching_a_child_finish_on_one_slot(db):
    model = Reactive(respond=reactive_respond)
    runner = runner_with(db, model, max_concurrent=1)

    results = await asyncio.wait_for(runner.run_many(Parent, [ParentInput(task=t) for t in "abcd"]), timeout=20)

    assert [r.output.answer for r in results] == list("abcd")
    assert model.max_in_flight == 1 and runner._slots._value == 1
    assert len(await db.list_runs()) == 8


async def test_several_launches_in_one_turn_hand_the_slot_back_once(db):
    def respond(user_text, call_number):
        payload = json.loads(user_text)
        if "topic" in payload:
            return child_done(payload["topic"])
        if call_number == 1:
            calls = [
                {
                    "name": "launch_agent",
                    "args": {"agent_name": "child", "input_json": json.dumps({"topic": t})},
                    "id": f"t-{t}",
                }
                for t in "ab"
            ]
            return AIMessage(content="", tool_calls=calls)
        return parent_done("both")

    model = Reactive(respond=respond)
    runner = runner_with(db, model, max_concurrent=2)

    result = await asyncio.wait_for(runner.run(Parent, PARENT_INPUT), timeout=10)

    assert result.status == RunStatus.SUCCESS and result.output.answer == "both"
    rows = await db.list_runs()
    assert sorted(r.input.get("topic", "") for r in rows if r.agent_name == "child") == ["a", "b"]
    assert runner._slots._value == 2  # not a permit leaked or lost


# the run log
