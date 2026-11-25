"""Background jobs: the result of a child comes back as a message, mid-loop and at the end of a run that is not a chat."""

import asyncio
import json
import time

import pytest
from fakes import FakeSettings, Scripted, Streams, runner_with, tool_call_chunk, tool_reply, use_model
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from test_background_jobs import (
    CHILD_ARGS,
    PARENT_INPUT,
    Child,
    Parent,
    child_done,
    finished,
    parent_done,
    start_child,
    tool_results,
)

from featherweight.background import BackgroundChildren, _would_end
from featherweight.client import JobClient
from featherweight.db import FINAL
from featherweight.model_settings import ModelSettings
from featherweight.models import Status
from featherweight.queue import SqliteQueue
from featherweight.runner import AgentRunner
from featherweight.streaming import ChatInput, StreamingAgent
from featherweight.testing import MemoryQueue, MemoryResultStore
from featherweight.worker import JobWorker


class Delayed(Scripted):
    """A scripted model that takes `delay_s` to answer, in a thread, so that the event loop (and a child that runs there) goes on."""

    delay_s: float = 0.0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(self.delay_s)
        return super()._generate(messages, stop, run_manager, **kwargs)


def schema_call(call_id="t-schema"):
    """A round of the loop that takes time and does nothing: the child finishes while it runs."""
    return tool_reply("get_agent_schema", {"agent_name": "child"}, call_id)


def results_in(messages) -> list[str]:
    return [m.content for m in messages if isinstance(m, HumanMessage) and "<background_result" in str(m.content)]


def assert_valid_order(messages: list[BaseMessage]) -> None:
    """What a provider asks of a conversation: every tool call has its result before anything else, and no result is an orphan."""
    open_calls: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            assert not open_calls, "a tool call had no result before the next message"
            open_calls = {call["id"] for call in message.tool_calls}
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in open_calls, "a tool result with no call"
            open_calls.discard(message.tool_call_id)
        else:
            assert not open_calls, "a message between a tool call and its result"
    assert not open_calls


@pytest.fixture
def results():
    return MemoryResultStore()


# the loop adds the results of the children that ended, once, after the round's tool results


async def test_a_child_that_ends_mid_loop_is_given_once_after_the_tool_results(db, results):
    use_model(Scripted(script=[child_done("found it")]), "child")
    model = Delayed(script=[start_child(), schema_call(), parent_done()], delay_s=0.3)
    runner = runner_with(db, model, results=results)

    result = await runner.run(Parent, PARENT_INPUT)

    assert result.status == Status.SUCCESS and model.calls == 3
    last = model.seen[2]
    assert len(results_in(last)) == 1
    assert isinstance(last[-1], HumanMessage) and "<background_result" in str(last[-1].content)
    assert isinstance(last[-2], ToolMessage) and last[-2].tool_call_id == "t-schema"  # after the round's results
    job_id = json.loads(tool_results(model)[0])["job_id"]
    assert f'job_id="{job_id}"' in last[-1].content and 'agent="child"' in last[-1].content
    assert 'status="SUCCESS"' in last[-1].content and '"summary": "found it"' in last[-1].content
    for messages in model.seen:
        assert_valid_order(messages[1:])  # after the system prompt


async def test_two_children_that_end_in_one_round_are_both_given_each_once(db, results):
    use_model(Scripted(script=[child_done("a"), child_done("b")]), "child")
    two = AIMessage(
        content="",
        tool_calls=[
            {"name": "start_background", "args": CHILD_ARGS, "id": "t1"},
            {"name": "start_background", "args": CHILD_ARGS, "id": "t2"},
        ],
    )
    model = Delayed(script=[two, schema_call(), parent_done()], delay_s=0.3)
    runner = runner_with(db, model, results=results)

    result = await runner.run(Parent, PARENT_INPUT)

    assert result.status == Status.SUCCESS
    ids = [json.loads(text)["job_id"] for text in tool_results(model)[:2]]
    given = results_in(model.seen[2])
    assert len(given) == 2 and len(set(ids)) == 2
    assert [f'job_id="{job_id}"' in text for job_id, text in zip(ids, given, strict=True)] == [
        True,
        True,
    ]  # in start order
    for messages in model.seen:
        assert_valid_order(messages[1:])


async def test_a_child_that_failed_is_given_with_its_error_type(db, results):
    use_model(Scripted(script=[RuntimeError("boom")] * 3), "child")
    model = Delayed(script=[start_child(), schema_call(), parent_done()], delay_s=0.3)
    runner = runner_with(db, model, results=results)

    result = await runner.run(Parent, PARENT_INPUT)

    assert result.status == Status.SUCCESS  # the parent goes on: a failed child is a result like any other
    (given,) = results_in(model.seen[2])
    assert (
        'status="FAILED"' in given and 'error_type="' in given and "boom" not in given
    )  # the type, and never the text
    assert "\n" not in given  # no output


async def test_a_child_that_has_not_ended_is_not_given(db, results):
    use_model(Delayed(script=[child_done()], delay_s=1.0), "child")
    model = Scripted(script=[start_child(), schema_call(), schema_call("t-2"), parent_done(), parent_done()])
    runner = runner_with(db, model, results=results)

    result = await runner.run(Parent, PARENT_INPUT)  # it waits for the child at the end

    assert result.status == Status.SUCCESS
    assert [len(results_in(messages)) for messages in model.seen] == [0, 0, 0, 0, 1]


# runner.background_message: the same message, for an application that starts the next turn


async def test_background_message_says_how_a_job_ended(db, results):
    use_model(Scripted(script=[child_done("found it")]), "child")
    model = Scripted(script=[start_child(), parent_done(), parent_done()])
    runner = runner_with(db, model, results=results)
    await runner.run(Parent, PARENT_INPUT)
    job_id = json.loads(tool_results(model)[0])["job_id"]

    message = await runner.background_message(job_id)

    assert isinstance(message, HumanMessage)
    assert message.content.startswith(f'<background_result job_id="{job_id}" agent="child" status="SUCCESS">')
    body = message.content.split("\n")[1]
    assert json.loads(body)["summary"] == "found it" and message.content.endswith("</background_result>")
    assert (
        message.content == results_in(model.seen[-1])[0]
    )  # what the loop gave the model is what the application builds


async def test_background_message_for_a_result_that_is_gone_says_so(db, results):
    use_model(Scripted(script=[child_done()]), "child")
    model = Scripted(script=[start_child(), parent_done(), parent_done()])
    runner = runner_with(db, model, results=results)
    await runner.run(Parent, PARENT_INPUT)
    job_id = json.loads(tool_results(model)[0])["job_id"]
    await results.delete_job(job_id)

    message = await runner.background_message(job_id)

    assert 'status="SUCCESS"' in message.content and 'output="missing"' in message.content
    assert message.content.endswith("/>")


async def test_background_message_for_a_failed_a_cancelled_and_a_running_job(db, results):
    runner = runner_with(db, Scripted(script=[]), results=results)
    failed = await db.create_job("child", job_hash="h", input_hash="i")
    await db.set_job_status(failed, Status.RUNNING)
    await db.set_job_status(failed, Status.FAILED, error_type="ValueError", error_hash="e" * 64)
    cancelled = await db.create_job("child", job_hash="h", input_hash="i")
    assert await db.cancel_queued_job(cancelled)
    waiting = await db.create_job("child", job_hash="h", input_hash="i")

    assert 'status="FAILED" error_type="ValueError" />' in (await runner.background_message(failed)).content
    assert (await runner.background_message(cancelled)).content.endswith('status="CANCELLED" />')
    assert (await runner.background_message(waiting)).content.endswith('status="INIT" />')
    with pytest.raises(LookupError):
        await runner.background_message("nobody")


# a run that is not a chat waits for its children


async def test_a_run_that_is_not_a_chat_waits_for_its_child_and_goes_on_with_the_result(db, results):
    use_model(Delayed(script=[child_done("late")], delay_s=0.3), "child")
    model = Scripted(script=[start_child(), parent_done("early"), parent_done("with the result")])
    runner = runner_with(
        db, model, results=results, max_concurrent=1
    )  # the parent gives its slot up: the child needs it

    result = await asyncio.wait_for(runner.run(Parent, PARENT_INPUT), timeout=20)

    assert result.status == Status.SUCCESS and model.calls == 3
    assert (
        result.output is not None and result.output.answer == "with the result"
    )  # the answer that came after the result
    assert len(results_in(model.seen[2])) == 1 and '"summary": "late"' in model.seen[2][-1].content
    assert len(results_in(model.seen[1])) == 0
    for messages in model.seen:
        assert_valid_order(messages[1:])


async def test_a_run_that_is_not_a_chat_does_not_wait_when_no_child_is_left(db, results):
    use_model(Scripted(script=[child_done()]), "child")
    model = Delayed(script=[start_child(), schema_call(), parent_done()], delay_s=0.3)
    runner = runner_with(db, model, results=results)

    await runner.run(Parent, PARENT_INPUT)

    assert model.calls == 3  # the child was given in the loop, so the end had nothing to wait for


async def test_a_run_waits_for_every_child_it_started(db, results):
    use_model(Delayed(script=[child_done("a"), child_done("b")], delay_s=0.2), "child")
    model = Scripted(
        script=[start_child("t1"), start_child("t2"), parent_done("0"), parent_done("1"), parent_done("2")],
    )
    runner = runner_with(db, model, results=results)

    result = await runner.run(Parent, PARENT_INPUT)

    assert result.status == Status.SUCCESS
    given = [text for messages in model.seen for text in results_in(messages)]
    assert len(set(given)) == 2  # both were given, and neither twice to the same call
    assert (await db.list_job_events(json.loads(tool_results(model)[0])["job_id"]))[-1].type == "succeeded"


async def test_a_child_on_a_worker_is_waited_for_too(db):
    queue = MemoryQueue()
    results = MemoryResultStore()
    use_model(Delayed(script=[child_done("far away")], delay_s=0.2), "child")
    client = JobClient(db, queue, results)
    worker = JobWorker(db, queue, AgentRunner(db, results=results), jobs=[Child], results=results)
    running = asyncio.create_task(worker.run())
    try:
        model = Scripted(script=[start_child(), parent_done("early"), parent_done("late")])
        runner = runner_with(db, model, results=results, background=client)
        result = await asyncio.wait_for(runner.run(Parent, PARENT_INPUT), timeout=20)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)

    assert result.output is not None and result.output.answer == "late"
    assert '"summary": "far away"' in model.seen[2][-1].content


@pytest.mark.parametrize("make_queue", [MemoryQueue, SqliteQueue])
async def test_the_message_of_a_child_on_a_worker_is_built_from_the_clients_store(db, make_queue):
    if make_queue is SqliteQueue and type(db).__name__ != "SqliteDatabase":
        pytest.skip("SqliteQueue needs the sqlite database")
    queue = make_queue() if make_queue is MemoryQueue else make_queue(db)
    results = MemoryResultStore()
    use_model(Scripted(script=[child_done("here")]), "child")
    client = JobClient(db, queue, results)
    worker = JobWorker(db, queue, AgentRunner(db, results=results), jobs=[Child], results=results)
    running = asyncio.create_task(worker.run())
    try:
        model = Scripted(script=[start_child(), parent_done(), parent_done()])
        runner = runner_with(db, model, results=results, background=client)
        await runner.run(Parent, PARENT_INPUT)
        job_id = json.loads(tool_results(model)[0])["job_id"]
        message = await runner.background_message(job_id)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)

    assert 'status="SUCCESS"' in message.content and '"summary": "here"' in message.content


# a chat turn does not wait


class ChatParent(StreamingAgent):
    name: str = "chat_parent"
    description: str = "Talks, and starts a child in the background"
    version: str = "1"
    prompt: str = "You are a chat agent."
    model: ModelSettings = FakeSettings()
    background_jobs: list[type] = [Child]


def say(text: str) -> ChatInput:
    from ag_ui.core import UserMessage

    return ChatInput(messages=[UserMessage(id="m1", content=text)], thread_id="t-1")


async def test_a_chat_turn_ends_without_waiting_and_the_child_goes_on(db, results):
    use_model(Delayed(script=[child_done("slow")], delay_s=0.6), "child")
    model = Streams(turns=[[tool_call_chunk("start_background", CHILD_ARGS, "c1")], ["Started."]])
    runner = runner_with(db, model, results=results)

    run = runner.stream(ChatParent, say("go"))
    events = [event async for event in run]

    started = next(e for e in events if e.type.value == "TOOL_CALL_RESULT")
    job_id = json.loads(started.content)["job_id"]
    row = await db.get_job(job_id)
    assert row is not None and row.status not in FINAL  # the turn is over, and the child is not
    assert model.calls == 2  # no extra turn to give a result that is not there
    await finished(runner)
    message = await runner.background_message(job_id)  # the application starts the next turn with this
    assert 'status="SUCCESS"' in message.content and '"summary": "slow"' in message.content


async def test_a_chat_turn_gets_the_result_of_a_child_that_ends_while_it_runs_and_keeps_it(db, results):
    use_model(Scripted(script=[child_done("quick")]), "child")
    model = Streams(
        turns=[
            [tool_call_chunk("start_background", CHILD_ARGS, "c1")],
            [tool_call_chunk("get_agent_schema", {"agent_name": "child"}, "c2")],
            ["Done."],
        ],
        delay_s=0.25,
    )
    runner = runner_with(db, model, results=results)

    run = runner.stream(ChatParent, say("go"))
    events = [event async for event in run]

    assert events[-1].type.value == "RUN_FINISHED"
    last = model.seen[2]
    assert len(results_in(last)) == 1 and isinstance(last[-2], ToolMessage) and last[-2].tool_call_id == "c2"
    for messages in model.seen:
        assert_valid_order(messages[1:])
    kept = [m for m in run.new_messages if isinstance(m, HumanMessage)]  # the conversation the application keeps has it
    assert len(kept) == 1 and "<background_result" in str(kept[0].content)


# the helpers


def _ai(*call_ids: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": i} for i in call_ids])


def test_the_loop_ends_when_every_tool_call_has_its_result():
    assert _would_end([HumanMessage(content="hi"), AIMessage(content="done")])
    assert _would_end(
        [_ai("a"), ToolMessage(content="r", tool_call_id="a")]
    )  # the structured answer is answered at once
    assert not _would_end([_ai("a", "b"), ToolMessage(content="r", tool_call_id="a")])
    assert not _would_end([_ai("a")])
    assert not _would_end([HumanMessage(content="hi")])


async def test_the_children_of_an_attempt_are_counted_and_given_once(db, results):
    job_id = await db.create_job("child", job_hash="h", input_hash="i")
    children = BackgroundChildren(db, results)
    children.add(job_id)
    assert await children.live() == 1 and await children.collect() == []
    assert await db.cancel_queued_job(job_id)
    assert await children.live() == 0
    assert len(await children.collect()) == 1 and await children.collect() == []  # once
    assert children.outstanding() == []
