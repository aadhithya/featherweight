"""Stopping a streamed response: the stream ends cleanly, the partial turn is kept and valid, the slot is free."""

import asyncio

import pytest
from ag_ui.core import UserMessage
from fakes import FakeSettings, Hold, Reactive, Streams, raw_call_chunk, runner_with, tool_call_chunk, use_model
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool
from stream_check import assert_valid

from micro.model_settings import ModelSettings
from micro.models import RunStatus
from micro.streaming import ChatInput, StreamingAgent


class Chat(StreamingAgent):
    name: str = "chat"
    description: str = "Talks to the user"
    version: str = "1"
    prompt: str = "You are a helpful chat agent."
    model: ModelSettings = FakeSettings()


def say(text: str = "hi") -> ChatInput:
    return ChatInput(messages=[UserMessage(id="u", content=text)], thread_id="t-1")


def kinds(events) -> list[str]:
    return [e.type.value for e in events]


async def stop_at(run, kind: str):
    """Loop over the run, stop it when the first event of this kind arrives, and return all the events."""
    events = []
    async for event in run:
        events.append(event)
        if event.type.value == kind:
            run.cancel()
    assert_valid(events)
    return events


def thought(text, **extra):
    block = {"type": "thinking", "thinking": text, "index": 0, **extra}
    return AIMessageChunk(content=[block], response_metadata={"model_provider": "anthropic"})


def assert_sendable(messages):
    """Every tool call has its result right after the message that made it: what a provider needs."""
    for i, message in enumerate(messages):
        if isinstance(message, AIMessage):
            answered = {m.tool_call_id for m in messages[i + 1 :] if isinstance(m, ToolMessage)}
            assert {c["id"] for c in message.tool_calls} <= answered, "a tool call has no result"


# an explicit cancel ends the stream cleanly


async def test_cancelling_while_text_streams_ends_the_stream_cleanly_and_keeps_the_text(db):
    gate = Hold()
    runner = runner_with(db, Streams(turns=[["Let me ", "think about ", gate, "that."]]))
    run = runner.stream(Chat, say())

    async def canceller():
        await gate.reached.wait()
        await asyncio.sleep(0.03)  # the chunks written so far are read
        run.cancel()

    task = asyncio.create_task(canceller())
    events = [e async for e in run]
    await task
    assert_valid(events)

    assert kinds(events)[-3:] == ["TEXT_MESSAGE_END", "STEP_FINISHED", "RUN_FINISHED"]
    assert events[-1].outcome.type == "cancelled"
    assert "".join(e.delta for e in events if e.type.value == "TEXT_MESSAGE_CONTENT") == "Let me think about "
    assert run.status is RunStatus.CANCELLED and run.error is None
    row = await db.get_run(run.run_id)
    assert row.status is RunStatus.CANCELLED and row.output == {"chars": 19} and row.error is None
    assert [(type(m), m.content) for m in run.new_messages] == [(AIMessage, "Let me think about ")]


async def test_runner_cancel_stops_a_run_by_its_id(db):
    gate = Hold()
    runner = runner_with(db, Streams(turns=[["a", gate, "b"]]))
    run = runner.stream(Chat, say())
    events = []

    async def read():
        async for event in run:
            events.append(event)

    reader = asyncio.create_task(read())
    await gate.reached.wait()
    assert runner.cancel(run.run_id) is True
    await reader
    assert events[-1].outcome.type == "cancelled"
    assert runner.cancel(run.run_id) is False  # it is over
    assert runner.cancel("no-such-run") is False
    assert (await db.get_run(run.run_id)).status is RunStatus.CANCELLED


async def test_cancelling_twice_or_after_the_end_does_nothing(db):
    runner = runner_with(db, Streams(turns=[["done"]]))
    run = runner.stream(Chat, say())
    events = [e async for e in run]
    run.cancel()
    run.cancel()
    assert events[-1].outcome.type == "success" and run.status is RunStatus.SUCCESS


async def test_cancelling_before_the_loop_starts_makes_a_cancelled_run(db):
    model = Streams(turns=[["never"]])
    runner = runner_with(db, model)
    run = runner.stream(Chat, say())
    run.cancel()
    events = [e async for e in run]
    assert_valid(events)
    assert kinds(events) == ["RUN_STARTED", "RUN_FINISHED"] and events[-1].outcome.type == "cancelled"
    assert model.calls == 0 and run.new_messages == []
    assert (await db.get_run(run.run_id)).status is RunStatus.CANCELLED


async def test_cancelling_while_waiting_for_a_slot(db):
    first_gate = Hold()
    runner = runner_with(db, Streams(turns=[["a", first_gate, "b"]]), max_concurrent=1)
    first = runner.stream(Chat, say("one"))
    second = runner.stream(Chat, say("two"))
    first_events, second_events = [], []

    async def read(run, into):
        async for event in run:
            into.append(event)

    task_a = asyncio.create_task(read(first, first_events))
    await first_gate.reached.wait()
    task_b = asyncio.create_task(read(second, second_events))
    for _ in range(50):  # the second run is waiting for the slot
        await asyncio.sleep(0.01)
        if (await db.get_run(second.run_id)) is not None:
            break
    assert (await db.get_run(second.run_id)).status is RunStatus.INIT
    second.cancel()
    await task_b
    assert kinds(second_events) == ["RUN_STARTED", "RUN_FINISHED"] and second_events[-1].outcome.type == "cancelled"
    assert (await db.get_run(second.run_id)).status is RunStatus.CANCELLED
    first_gate.release.set()
    await task_a
    assert first_events[-1].outcome.type == "success"  # the first run was not touched


# the partial turn


async def test_a_tool_call_whose_arguments_were_still_streaming_is_dropped(db):
    gate = Hold()
    runner = runner_with(db, Streams(turns=[[raw_call_chunk("weather", '{"city": "Pa', "c1"), gate, "rest"]]))
    run = runner.stream(Chat, say())
    events = []

    async def read():
        async for event in run:
            events.append(event)

    reader = asyncio.create_task(read())
    await gate.reached.wait()
    await asyncio.sleep(0.03)  # the chunks written so far are read
    run.cancel()
    await reader
    assert_valid(events)
    assert kinds(events)[-3:] == ["TOOL_CALL_END", "STEP_FINISHED", "RUN_FINISHED"]  # what was open is closed
    assert run.new_messages == []  # nothing complete to keep, and nothing that would be rejected


async def test_a_complete_tool_call_that_had_not_returned_gets_a_result_that_says_so(db):
    started, stop = Hold(), Hold()
    ran = []

    @tool
    async def slow(city: str) -> str:
        """A slow tool."""
        ran.append(city)
        started.reached.set()
        try:
            await stop.release.wait()
        except asyncio.CancelledError:
            ran.append("cancelled")
            raise
        return "late"

    class WithTool(Chat):
        tools: list = [slow]

    runner = runner_with(db, Streams(turns=[[tool_call_chunk("slow", {"city": "Paris"}, "c1")], ["never"]]))
    run = runner.stream(WithTool, say())
    events = []

    async def read():
        async for event in run:
            events.append(event)

    reader = asyncio.create_task(read())
    await started.reached.wait()
    await asyncio.sleep(0.03)  # the chunks written so far are read
    run.cancel()
    await reader
    assert_valid(events)
    assert ran == ["Paris", "cancelled"]  # an async tool is stopped where it waits
    ai, result = run.new_messages
    assert isinstance(ai, AIMessage) and ai.tool_calls[0]["id"] == "c1"
    assert isinstance(result, ToolMessage) and result.tool_call_id == "c1" and result.status == "error"
    assert "did not return" in result.content and "cancelled by the user" in result.content
    assert_sendable(run.new_messages)


async def test_a_tool_that_returned_keeps_its_real_result_and_the_text_after_it(db):
    gate = Hold()

    @tool
    def weather(city: str) -> str:
        """Weather."""
        return f"sunny in {city}"

    class WithTool(Chat):
        tools: list = [weather]

    runner = runner_with(
        db, Streams(turns=[[tool_call_chunk("weather", {"city": "Paris"}, "c1")], ["It is ", gate, "sunny"]])
    )
    run = runner.stream(WithTool, say())
    events = []

    async def read():
        async for event in run:
            events.append(event)

    reader = asyncio.create_task(read())
    await gate.reached.wait()
    await asyncio.sleep(0.03)  # the chunks written so far are read
    run.cancel()
    await reader
    assert_valid(events)
    ai, result, partial = run.new_messages
    assert result.content == "sunny in Paris" and result.status == "success"
    assert isinstance(partial, AIMessage) and partial.content == "It is "
    assert_sendable(run.new_messages)


async def test_a_thought_is_kept_only_with_its_signature(db):
    class Thinker(Chat):
        model: ModelSettings = FakeSettings(show_thoughts=True)

    gate = Hold()
    signed = Streams(turns=[[thought("Hmm. "), thought("", signature="SIG"), "Answer", gate, "more"]])
    run = runner_with(db, signed).stream(Thinker, say())
    reader = asyncio.create_task(_drain(run))
    await gate.reached.wait()
    await asyncio.sleep(0.03)  # the chunks written so far are read
    run.cancel()
    await reader
    (partial,) = run.new_messages
    assert isinstance(partial.content, list)
    reasoning = [b for b in partial.content if b["type"] == "reasoning"]
    assert reasoning and reasoning[0]["extras"] == {"signature": "SIG"} and reasoning[0]["reasoning"] == "Hmm. "
    assert [b["text"] for b in partial.content if b["type"] == "text"] == ["Answer"]

    unsigned_gate = Hold()
    unsigned = Streams(turns=[[thought("Hmm. "), "Answer", unsigned_gate, "more"]])
    run2 = runner_with(db, unsigned).stream(Thinker, say())
    reader = asyncio.create_task(_drain(run2))
    await unsigned_gate.reached.wait()
    await asyncio.sleep(0.03)  # the chunks written so far are read
    run2.cancel()
    await reader
    assert [m.content for m in run2.new_messages] == ["Answer"]  # a thought with no signature would be rejected


async def _drain(run):
    return [e async for e in run]


# the reader leaves


async def test_a_reader_that_leaves_cancels_the_run_and_the_partial_turn_is_kept(db):
    gate = Hold()
    runner = runner_with(db, Streams(turns=[["Half ", "a ", gate, "sentence"]]), max_concurrent=1)
    run = runner.stream(Chat, say())
    stream = aiter(run)
    seen = []
    async for event in stream:
        seen.append(event)
        if event.type.value == "TEXT_MESSAGE_CONTENT" and event.delta == "a ":
            break
    await stream.aclose()  # a web framework does this when the client goes away
    for _ in range(100):
        if (await db.get_run(run.run_id)).status is RunStatus.CANCELLED:
            break
        await asyncio.sleep(0.01)
    assert run.status is RunStatus.CANCELLED
    assert [m.content for m in run.new_messages] == ["Half a "]
    second = runner_with(db, Streams(turns=[["fine"]]), max_concurrent=1)
    assert [e async for e in second.stream(Chat, say("again"))][-1].outcome.type == "success"  # the slot is free


# the user corrects the agent


async def test_the_next_turn_after_a_stop_is_valid_and_has_what_the_agent_did(db):
    started = Hold()

    @tool
    async def send_email(to: str) -> str:
        """Send an email."""
        started.reached.set()
        await asyncio.Event().wait()
        return "sent"

    class Mailer(Chat):
        tools: list = [send_email]

    first_model = Streams(turns=[["Sending it now. ", tool_call_chunk("send_email", {"to": "bob"}, "c1")]])
    runner = runner_with(db, first_model)
    run = runner.stream(Mailer, ChatInput(messages=[UserMessage(id="u", content="email Bob")]))
    reader = asyncio.create_task(_drain(run))
    await started.reached.wait()
    await asyncio.sleep(0.03)  # the chunks written so far are read
    run.cancel()
    await reader

    history = [HumanMessage(content="email Bob"), *run.new_messages]
    assert_sendable(history)
    second_model = Streams(turns=[["Understood, I will not."]])
    second = runner_with(db, second_model)
    follow_up = ChatInput.from_langchain([*history, HumanMessage(content="no, wait, do not send that")])
    events = [e async for e in second.stream(Mailer, follow_up)]
    assert events[-1].outcome.type == "success"
    sent = second_model.seen[0]
    assert_sendable(list(sent))
    assert any(isinstance(m, ToolMessage) and "did not return" in m.content for m in sent)
    assert any(isinstance(m, AIMessage) and m.tool_calls for m in sent)  # the model is told it tried to send it


# a failure leaves a valid history too


async def test_a_failure_while_a_tool_call_streams_leaves_a_valid_history(db):
    runner = runner_with(
        db, Streams(turns=[["Checking. ", raw_call_chunk("weather", '{"city"', "c1"), RuntimeError("down")]])
    )
    run = runner.stream(Chat, say())
    events = await _drain(run)
    assert events[-1].type.value == "RUN_ERROR"
    assert [m.content for m in run.new_messages] == ["Checking. "]
    assert_sendable(run.new_messages)


# a launched agent goes with its parent


async def test_a_launched_agent_is_cancelled_with_its_parent_and_its_row_says_so(db):
    from fakes import Echo

    gate = Hold()
    use_model(Streams(turns=[[gate, tool_call_chunk("EchoOutput", {"echoed": "x"}, "cc")]]), "child")

    class Child(Echo):
        model: ModelSettings = FakeSettings(key="child")

    class Parent(Chat):
        subagents: list[type] = [Child]

    launch = tool_call_chunk("launch_agent", {"agent_name": "echo", "input_json": '{"text": "x"}'}, "l1")
    runner = runner_with(db, Streams(turns=[[launch], ["done"]]), max_concurrent=2)
    run = runner.stream(Parent, say())
    reader = asyncio.create_task(_drain(run))
    await gate.reached.wait()
    await asyncio.sleep(0.03)  # the chunks written so far are read
    run.cancel()
    await reader
    rows = {r.agent_name: r for r in await db.list_runs()}
    assert rows["chat"].status is RunStatus.CANCELLED
    assert rows["echo"].status is RunStatus.CANCELLED  # not left RUNNING
    assert_sendable(run.new_messages)


# the same for a run() that is cancelled


async def test_a_run_whose_task_is_cancelled_is_marked_cancelled(db):
    from fakes import Echo, EchoInput, respond

    runner = runner_with(db, Reactive(respond=respond, delay_s=5))
    task = asyncio.create_task(runner.run(Echo, EchoInput(text="ok-1")))
    for _ in range(100):
        await asyncio.sleep(0.01)
        rows = await db.list_runs()
        if rows and rows[0].status is RunStatus.RUNNING:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r.status for r in await db.list_runs()] == [RunStatus.CANCELLED]
    assert (
        await runner_with(db, Reactive(respond=respond, delay_s=0), max_concurrent=1).run(Echo, EchoInput(text="ok-2"))
    ).status is RunStatus.SUCCESS
