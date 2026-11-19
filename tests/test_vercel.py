"""The Vercel AI SDK adapter: the request into a ChatInput, the events into the UI message stream. Fake models only."""

import asyncio
import json

import pytest
from ag_ui.core import (
    AssistantMessage,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from fakes import FakeSettings, Streams, runner_with, tool_call_chunk
from langchain_core.tools import tool

from featherweight.adapters.errors import BadRequest
from featherweight.adapters.vercel import (
    DONE,
    HEADERS,
    Translator,
    encode,
    parse_request,
    to_agui_messages,
    translate,
)
from featherweight.db import Database
from featherweight.model_settings import ModelSettings
from featherweight.models import RunStatus
from featherweight.streaming import StreamingAgent


class Chat(StreamingAgent):
    name: str = "chat"
    description: str = "Talks to the user"
    version: str = "1"
    prompt: str = "You are a helpful chat agent."
    model: ModelSettings = FakeSettings()


def parts(sse: list[str]) -> list[dict]:
    """The parts of a list of server-sent events (without the end marker)."""
    assert sse[-1] == DONE
    return [json.loads(line.removeprefix("data: ")) for line in sse[:-1]]


def user(text, id="u1"):
    return {"id": id, "role": "user", "parts": [{"type": "text", "text": text}]}


# the response: the examples of the protocol page


def test_a_text_turn_is_the_parts_of_the_protocol_page():
    events = [
        RunStartedEvent(thread_id="t", run_id="msg-1"),
        StepStartedEvent(step_name="model"),
        TextMessageStartEvent(message_id="m1", role="assistant"),
        TextMessageContentEvent(message_id="m1", delta="Hello"),
        TextMessageEndEvent(message_id="m1"),
        StepFinishedEvent(step_name="model"),
        RunFinishedEvent(thread_id="t", run_id="msg-1"),
    ]
    assert parts(translate(events)) == [
        {"type": "start", "messageId": "msg-1"},
        {"type": "start-step"},
        {"type": "text-start", "id": "m1"},
        {"type": "text-delta", "id": "m1", "delta": "Hello"},
        {"type": "text-end", "id": "m1"},
        {"type": "finish-step"},
        {"type": "finish"},
    ]


def test_the_wire_format_is_compact_sse_with_the_end_marker():
    sse = translate([RunStartedEvent(thread_id="t", run_id="r"), RunFinishedEvent(thread_id="t", run_id="r")])
    assert sse[0] == 'data: {"type":"start","messageId":"r"}\n\n'
    assert sse[-1] == "data: [DONE]\n\n"
    assert HEADERS["x-vercel-ai-ui-message-stream"] == "v1" and HEADERS["Content-Type"] == "text/event-stream"


def test_thoughts_become_reasoning_parts_and_the_opaque_value_is_dropped():
    events = [
        ReasoningStartEvent(message_id="r1"),
        ReasoningMessageStartEvent(message_id="r1", role="reasoning"),
        ReasoningMessageContentEvent(message_id="r1", delta="This is some reasoning"),
        ReasoningMessageEndEvent(message_id="r1"),
        ReasoningEndEvent(message_id="r1"),
    ]
    assert parts(translate(events)) == [
        {"type": "reasoning-start", "id": "r1"},
        {"type": "reasoning-delta", "id": "r1", "delta": "This is some reasoning"},
        {"type": "reasoning-end", "id": "r1"},
    ]


def test_a_tool_call_is_the_input_parts_and_the_output_of_the_protocol_page():
    events = [
        ToolCallStartEvent(tool_call_id="c1", tool_call_name="getWeatherInformation"),
        ToolCallArgsEvent(tool_call_id="c1", delta='{"city":'),
        ToolCallArgsEvent(tool_call_id="c1", delta='"San Francisco"}'),
        ToolCallEndEvent(tool_call_id="c1"),
        ToolCallResultEvent(
            message_id="t1", tool_call_id="c1", content='{"city": "San Francisco", "weather": "sunny"}'
        ),
    ]
    assert parts(translate(events)) == [
        {"type": "tool-input-start", "toolCallId": "c1", "toolName": "getWeatherInformation"},
        {"type": "tool-input-delta", "toolCallId": "c1", "inputTextDelta": '{"city":'},
        {"type": "tool-input-delta", "toolCallId": "c1", "inputTextDelta": '"San Francisco"}'},
        {
            "type": "tool-input-available",
            "toolCallId": "c1",
            "toolName": "getWeatherInformation",
            "input": {"city": "San Francisco"},
        },
        {"type": "tool-output-available", "toolCallId": "c1", "output": {"city": "San Francisco", "weather": "sunny"}},
    ]


def test_a_tool_with_no_arguments_has_an_empty_input_and_a_text_result_stays_text():
    events = [
        ToolCallStartEvent(tool_call_id="c1", tool_call_name="now"),
        ToolCallEndEvent(tool_call_id="c1"),
        ToolCallResultEvent(message_id="t1", tool_call_id="c1", content="sunny in Paris"),
    ]
    out = parts(translate(events))
    assert out[1]["input"] == {} and out[2]["output"] == "sunny in Paris"


def test_a_cancelled_run_is_an_abort_and_an_error_is_an_error_part():
    cancelled = RunFinishedEvent(thread_id="t", run_id="r", outcome=RunFinishedCancelledOutcome())
    assert parts(translate([cancelled])) == [{"type": "abort", "reason": "stopped by the user"}]
    assert parts(translate([RunErrorEvent(message="provider down")])) == [
        {"type": "error", "errorText": "provider down"}
    ]


def test_events_with_no_part_are_dropped():
    assert Translator().feed(StateSnapshotEvent(snapshot={"a": 1})) == []


# the response, from a real run


async def test_a_run_is_encoded_as_it_streams(db):
    @tool
    def weather(city: str) -> str:
        """The weather in a city."""
        return f"sunny in {city}"

    class WithTool(Chat):
        tools: list = [weather]

    model = Streams(turns=[[tool_call_chunk("weather", {"city": "Paris"}, "c1")], ["It is ", "sunny."]])
    runner = runner_with(db, model)
    sse = [
        piece
        async for piece in encode(
            runner.stream(WithTool, parse_request({"id": "chat-1", "messages": [user("weather?")]}))
        )
    ]
    out = parts(sse)

    assert [p["type"] for p in out] == [
        "start",
        "start-step",
        "tool-input-start",
        "tool-input-delta",
        "tool-input-available",
        "tool-output-available",
        "finish-step",
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
    ]
    assert out[4]["input"] == {"city": "Paris"} and out[5]["output"] == "sunny in Paris"
    assert "".join(p["delta"] for p in out if p["type"] == "text-delta") == "It is sunny."
    assert [r.thread_id for r in await db.list_runs()] == ["chat-1"]


async def test_a_run_that_fails_ends_with_an_error_part_and_the_end_marker(db):
    runner = runner_with(db, Streams(turns=[["Half", RuntimeError("provider down")]]))
    out = parts([piece async for piece in encode(runner.stream(Chat, parse_request([user("hi")])))])
    assert out[-1]["type"] == "error" and "provider down" in out[-1]["errorText"]


async def test_stopping_the_iteration_stops_the_run(db: Database):
    runner = runner_with(db, Streams(turns=[["a", "b", "c", "d"]], delay_s=0.02), max_concurrent=1)
    run = runner.stream(Chat, parse_request([user("hi")]))
    stream = encode(run)
    async for piece in stream:
        if '"text-delta"' in piece:
            break
    await stream.aclose()  # a web framework does this when the client goes away
    for _ in range(50):
        row = await db.get_run(run.run_id)
        if row.status is not RunStatus.RUNNING:
            break
        await asyncio.sleep(0.02)
    assert row.status is RunStatus.CANCELLED
    second = runner_with(db, Streams(turns=[["fine"]]), max_concurrent=1)
    assert parts([p async for p in encode(second.stream(Chat, parse_request([user("again")])))])[-1] == {
        "type": "finish"
    }


# the request


def test_the_request_of_use_chat_is_a_chat_input():
    body = {
        "id": "chat-9",
        "messages": [
            user("Hi"),
            {"id": "a1", "role": "assistant", "parts": [{"type": "text", "text": "Hello"}]},
            user("And?", "u2"),
        ],
        "trigger": "submit-message",
    }
    chat_input = parse_request(body)
    assert chat_input.thread_id == "chat-9"
    assert [(m.role, m.content) for m in chat_input.messages] == [
        ("user", "Hi"),
        ("assistant", "Hello"),
        ("user", "And?"),
    ]


def test_a_json_string_or_a_bare_list_is_read_too():
    assert parse_request(json.dumps({"messages": [user("Hi")]})).messages[0].content == "Hi"
    assert parse_request(json.dumps({"messages": [user("Hi")]}).encode()).thread_id is None
    assert parse_request([user("Hi")]).messages[0].content == "Hi"


def test_text_parts_are_joined_and_other_parts_are_ignored():
    ui = {
        "id": "u",
        "role": "user",
        "parts": [
            {"type": "text", "text": "one "},
            {"type": "file", "url": "x", "mediaType": "image/png"},
            {"type": "text", "text": "two"},
        ],
    }
    assert parse_request([ui]).messages[0].content == "one two"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not json", "not JSON"),
        ({"nope": 1}, "list of messages"),
        ([{"id": "s", "role": "system", "parts": []}, user("hi")], "system message"),
        ([{"id": "x", "role": "robot", "parts": []}], "role 'robot'"),
        ([{"id": "x", "role": "user"}], "list of parts"),
        (
            [user("hi"), {"id": "a", "role": "assistant", "parts": [{"type": "text", "text": "yes"}]}],
            "last message must be from the user",
        ),
        ([], "at least 1"),
    ],
)
def test_a_request_that_cannot_be_used_says_why(body, message):
    with pytest.raises(BadRequest, match=message):
        parse_request(body)


def test_an_assistant_message_with_tool_parts_becomes_the_messages_the_model_saw():
    ui = [
        user("weather in Paris?"),
        {
            "id": "a1",
            "role": "assistant",
            "parts": [
                {"type": "step-start"},
                {"type": "reasoning", "text": "I should look it up"},
                {
                    "type": "tool-weather",
                    "toolCallId": "c1",
                    "state": "output-available",
                    "input": {"city": "Paris"},
                    "output": {"temp": 21},
                },
                {
                    "type": "tool-lookup",
                    "toolCallId": "c2",
                    "state": "output-error",
                    "input": {"q": 1},
                    "errorText": "boom",
                },
                {"type": "step-start"},
                {"type": "text", "text": "It is 21 degrees."},
            ],
        },
    ]
    messages = to_agui_messages(ui)
    assert [m.role for m in messages] == ["user", "reasoning", "assistant", "tool", "tool", "assistant"]
    assistant = messages[2]
    assert isinstance(assistant, AssistantMessage) and assistant.tool_calls
    assert [(c.id, c.function.name, json.loads(c.function.arguments)) for c in assistant.tool_calls] == [
        ("c1", "weather", {"city": "Paris"}),
        ("c2", "lookup", {"q": 1}),
    ]
    assert messages[1].content == "I should look it up"
    assert (messages[3].tool_call_id, json.loads(messages[3].content)) == ("c1", {"temp": 21})
    assert (messages[4].tool_call_id, messages[4].error) == ("c2", "boom")
    assert messages[5].content == "It is 21 degrees." and not messages[5].tool_calls


def test_a_tool_whose_input_never_finished_is_left_out():
    ui = [
        {
            "id": "a",
            "role": "assistant",
            "parts": [{"type": "tool-weather", "toolCallId": "c1", "state": "input-streaming", "input": {"ci": "x"}}],
        }
    ]
    assert to_agui_messages(ui) == []


def test_a_dynamic_tool_part_names_its_tool():
    ui = [
        {
            "id": "a",
            "role": "assistant",
            "parts": [
                {
                    "type": "dynamic-tool",
                    "toolName": "mcp_search",
                    "toolCallId": "c1",
                    "state": "input-available",
                    "input": {},
                }
            ],
        }
    ]
    message = to_agui_messages(ui)[0]
    assert isinstance(message, AssistantMessage) and message.tool_calls
    assert message.tool_calls[0].function.name == "mcp_search"
