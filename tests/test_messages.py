"""AG-UI and LangChain messages: the converters, the round trips, and `new_messages`. Fake models only."""

import json

import pytest
from ag_ui.core import (
    ActivityMessage,
    AssistantMessage,
    FunctionCall,
    ReasoningMessage,
    RunAgentInput,
    ToolCall,
    UserMessage,
)
from ag_ui.core import ToolMessage as AgUiToolMessage
from fakes import FakeSettings, Streams, runner_with, tool_call_chunk
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import ValidationError

from micro.messages import to_agui, to_langchain
from micro.model_settings import ModelSettings
from micro.streaming import ChatInput, StreamingAgent


def call(call_id, name, args):
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(args)))


# AG-UI -> LangChain


def test_text_and_tool_calls_and_results_convert():
    messages = [
        UserMessage(id="u1", content="weather?"),
        AssistantMessage(id="a1", content="Let me look.", tool_calls=[call("c1", "weather", {"city": "Paris"})]),
        AgUiToolMessage(id="t1", content="sunny", tool_call_id="c1"),
        AssistantMessage(id="a2", content="It is sunny."),
    ]
    human, ai, result, final = to_langchain(messages)
    assert isinstance(human, HumanMessage) and human.content == "weather?" and human.id == "u1"
    assert ai.content == "Let me look." and ai.id == "a1"
    assert ai.tool_calls == [{"name": "weather", "args": {"city": "Paris"}, "id": "c1", "type": "tool_call"}]
    assert isinstance(result, ToolMessage) and result.tool_call_id == "c1" and result.status == "success"
    assert final.content == "It is sunny." and not final.tool_calls


def test_a_tool_error_becomes_a_tool_message_with_status_error():
    (_, result) = to_langchain(
        [UserMessage(id="u", content="x"), AgUiToolMessage(id="t", content="", tool_call_id="c1", error="boom")]
    )
    assert result.status == "error" and result.content == "boom"


def test_reasoning_goes_into_the_assistant_message_that_follows_it_with_its_signature():
    messages = [
        UserMessage(id="u", content="why?"),
        ReasoningMessage(id="r1", content="Because.", encrypted_value="SIG"),
        AssistantMessage(id="a", content="Because of X."),
    ]
    _, ai = to_langchain(messages)
    assert ai.content == [
        {"type": "reasoning", "reasoning": "Because.", "id": "r1", "extras": {"signature": "SIG"}},
        {"type": "text", "text": "Because of X."},
    ]


def test_an_activity_message_is_left_out():
    messages = [ActivityMessage(id="x", activity_type="ui", content={"a": 1}), UserMessage(id="u", content="hi")]
    assert [type(m) for m in to_langchain(messages)] == [HumanMessage]


def test_arguments_that_are_not_json_are_refused():
    bad = AssistantMessage(id="a", tool_calls=[ToolCall(id="c", function=FunctionCall(name="t", arguments="{nope"))])
    with pytest.raises(ValueError, match="not JSON"):
        to_langchain([bad])


# round trips


def test_ag_ui_to_langchain_to_ag_ui_keeps_everything():
    messages = [
        UserMessage(id="u1", content="do two things"),
        ReasoningMessage(id="r1", content="Plan.", encrypted_value="SIG"),
        AssistantMessage(
            id="a1",
            content="On it.",
            tool_calls=[call("c1", "a", {"x": 1}), call("c2", "b", {"y": [1, 2]})],  # several in one message
        ),
        AgUiToolMessage(id="t1", content="one", tool_call_id="c1"),
        AgUiToolMessage(id="t2", content="failed", tool_call_id="c2", error="failed"),  # LangChain has one text
        AssistantMessage(id="a2", tool_calls=[call("c3", "c", {})]),  # only a tool call, no text
        AgUiToolMessage(id="t3", content="three", tool_call_id="c3"),
        AssistantMessage(id="a3", content="Done."),
        UserMessage(id="u2", content="thanks"),
    ]
    assert to_agui(to_langchain(messages)) == messages


def test_langchain_to_ag_ui_to_langchain_keeps_everything():
    messages = [
        HumanMessage(content="hi", id="u1"),
        AIMessage(
            content=[
                {"type": "reasoning", "reasoning": "Hmm.", "id": "r1", "extras": {"signature": "SIG"}},
                {"type": "text", "text": "Calling."},
            ],
            tool_calls=[{"name": "t", "args": {"k": "v"}, "id": "c1", "type": "tool_call"}],
            id="a1",
        ),
        ToolMessage(content="result", tool_call_id="c1", id="t1"),
        ToolMessage(content="bad", tool_call_id="c2", status="error", id="t2"),
        AIMessage(content="Done.", id="a2"),
    ]
    again = to_langchain(to_agui(messages))
    assert [(type(m), m.id, m.content) for m in again] == [(type(m), m.id, m.content) for m in messages]
    assert again[1].tool_calls == messages[1].tool_calls
    assert [m.status for m in again if isinstance(m, ToolMessage)] == ["success", "error"]


def test_an_anthropic_message_with_thinking_becomes_a_reasoning_message():
    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "Let me see.", "signature": "SIG"},
            {"type": "text", "text": "Hello"},
        ],
        response_metadata={"model_provider": "anthropic"},
        id="a",
    )
    reasoning, assistant = to_agui([HumanMessage(content="hi", id="u"), message])[1:]
    assert (reasoning.content, reasoning.encrypted_value) == ("Let me see.", "SIG")
    assert assistant.content == "Hello"


# ChatInput


def test_chat_input_from_langchain_and_from_agui():
    history = [
        HumanMessage(content="hi", id="1"),
        AIMessage(content="hello", id="2"),
        HumanMessage(content="and?", id="3"),
    ]
    from_lc = ChatInput.from_langchain(history, thread_id="t")
    assert [m.role for m in from_lc.messages] == ["user", "assistant", "user"] and from_lc.thread_id == "t"
    request = RunAgentInput(thread_id="th", run_id="r", messages=from_lc.messages)
    assert ChatInput.from_agui(request).thread_id == "th"
    assert ChatInput.from_agui(from_lc.messages, thread_id="x").thread_id == "x"
    with pytest.raises(ValidationError, match="last message"):
        ChatInput.from_langchain(history[:2])


def test_a_history_with_tool_calls_and_thoughts_is_a_valid_input():
    ChatInput(
        messages=[
            UserMessage(id="1", content="hi"),
            ReasoningMessage(id="r", content="hm"),
            AssistantMessage(id="2", tool_calls=[call("c", "t", {})]),
            AgUiToolMessage(id="3", content="ok", tool_call_id="c"),
            UserMessage(id="4", content="thanks"),
        ]
    )


# new_messages


@tool
def weather(city: str) -> str:
    """The weather in a city."""
    return f"sunny in {city}"


class Weather(StreamingAgent):
    name: str = "weather"
    description: str = "x"
    version: str = "1"
    prompt: str = "x"
    model: ModelSettings = FakeSettings()
    tools: list = [weather]


async def test_new_messages_are_the_messages_of_the_turn(db):
    model = Streams(turns=[[tool_call_chunk("weather", {"city": "Paris"}, "c1")], ["It is sunny."]])
    runner = runner_with(db, model)
    run = runner.stream(Weather, ChatInput(messages=[UserMessage(id="u", content="weather in Paris?")]))
    assert run.new_messages == []
    [_ async for _ in run]

    ai, result, final = run.new_messages
    assert isinstance(ai, AIMessage) and ai.tool_calls[0]["name"] == "weather"
    assert isinstance(result, ToolMessage) and result.content == "sunny in Paris" and result.tool_call_id == "c1"
    assert isinstance(final, AIMessage) and final.text == "It is sunny."


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_the_conversation_is_kept_with_a_langchain_history(db):
    history = InMemoryChatMessageHistory()
    model = Streams(turns=[["Hello Sam."], ["Your name is Sam."]])
    runner = runner_with(db, model)

    async def turn(text):
        history.add_message(HumanMessage(content=text))
        run = runner.stream(Weather, ChatInput.from_langchain(history.messages, thread_id="t"))
        events = [e async for e in run]
        history.add_messages(run.new_messages)
        return events

    await turn("My name is Sam.")
    await turn("What is my name?")
    second_call = model.seen[1]
    assert [m.content for m in second_call[1:]] == ["My name is Sam.", "Hello Sam.", "What is my name?"]
    assert [type(m) for m in history.messages] == [HumanMessage, AIMessage, HumanMessage, AIMessage]


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_a_stored_conversation_with_tools_goes_back_to_the_model(db):
    history = InMemoryChatMessageHistory()
    model = Streams(turns=[[tool_call_chunk("weather", {"city": "Paris"}, "c1")], ["Sunny."], ["Yes, Paris."]])
    runner = runner_with(db, model)
    history.add_message(HumanMessage(content="weather in Paris?"))
    first = runner.stream(Weather, ChatInput.from_langchain(history.messages))
    [_ async for _ in first]
    history.add_messages(first.new_messages)
    history.add_message(HumanMessage(content="was that Paris?"))
    second = runner.stream(Weather, ChatInput.from_langchain(history.messages))
    [_ async for _ in second]

    sent = model.seen[2]  # the third model call: the second turn
    assert [type(m).__name__ for m in sent[1:]] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert sent[2].tool_calls[0]["id"] == "c1" and sent[3].tool_call_id == "c1"


async def test_a_failed_turn_keeps_the_messages_it_had(db):
    model = Streams(turns=[[tool_call_chunk("weather", {"city": "Paris"}, "c1")], [RuntimeError("down")]])
    runner = runner_with(db, model)
    run = runner.stream(Weather, ChatInput(messages=[UserMessage(id="u", content="weather?")]))
    events = [e async for e in run]
    assert events[-1].type.value == "RUN_ERROR"
    assert [type(m) for m in run.new_messages] == [AIMessage, ToolMessage]  # the call and its result happened
