"""The mapping of LangChain's chunks to AG-UI events, with recorded chunks. No model is called."""

from ag_ui.core import RunFinishedEvent, RunStartedEvent
from langchain_core.messages import AIMessageChunk, ToolMessage
from stream_check import assert_valid

from featherweight.stream_events import EventMapper


def run(chunks, *, show_thoughts=False):
    """Feed the chunks, close the run, and return its events between a start and a finish, which are checked."""
    mapper = EventMapper(show_thoughts=show_thoughts)
    events = []
    for chunk in chunks:
        events += mapper.feed(chunk)
    events += mapper.finish()
    assert_valid([RunStartedEvent(thread_id="t", run_id="r"), *events, RunFinishedEvent(thread_id="t", run_id="r")])
    return events, mapper


def kinds(events):
    return [e.type.value for e in events]


def text(chunk_text, turn="a", **kwargs):
    return AIMessageChunk(content=chunk_text, id=turn, **kwargs)


def call(name, args, call_id, turn="a", index=0):
    return AIMessageChunk(
        content="", id=turn, tool_call_chunks=[{"name": name, "args": args, "id": call_id, "index": index}]
    )


def anthropic(blocks, turn="a"):
    return AIMessageChunk(content=blocks, id=turn, response_metadata={"model_provider": "anthropic"})


# text


def test_text_chunks_share_one_message():
    events, _ = run([text("Hel"), text("lo")])
    assert kinds(events) == [
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STEP_FINISHED",
    ]
    assert events[1].message_id == events[2].message_id == events[3].message_id == events[4].message_id
    assert events[1].role == "assistant"
    assert [e.delta for e in events[2:4]] == ["Hel", "lo"]


def test_a_chunk_with_no_content_makes_no_events():
    events, _ = run([text("")])
    assert events == []


def test_the_closing_chunk_of_a_stream_does_not_start_a_turn():
    # It has no content, and its id can differ from the others', but its tokens count.
    usage = {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    events, mapper = run([text("Hi", turn="provider-id"), text("", turn="lc_run--x", usage_metadata=usage)])
    assert kinds(events).count("STEP_STARTED") == 1
    assert (mapper.input_tokens, mapper.output_tokens) == (1, 2)


def test_chunks_with_no_id_are_one_turn():
    events, _ = run([AIMessageChunk(content="a"), AIMessageChunk(content="b")])
    assert kinds(events).count("STEP_STARTED") == 1 and kinds(events).count("TEXT_MESSAGE_START") == 1


def test_a_new_model_turn_is_a_new_step_and_a_new_message():
    events, _ = run([text("one", turn="a"), text("two", turn="b")])
    assert kinds(events) == [
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STEP_FINISHED",
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STEP_FINISHED",
    ]
    assert events[1].message_id != events[6].message_id


def test_text_in_blocks_is_read():
    events, _ = run([anthropic([{"type": "text", "text": "Hi", "index": 0}])])
    assert [e.delta for e in events if e.type.value == "TEXT_MESSAGE_CONTENT"] == ["Hi"]


def test_a_text_block_that_starts_again_is_a_second_message():
    events, _ = run(
        [
            anthropic([{"type": "text", "text": "one", "index": 0}]),
            anthropic([{"type": "text", "text": "two", "index": 2}]),
        ]
    )
    assert kinds(events).count("TEXT_MESSAGE_START") == 2


def test_tokens_are_counted():
    usage = {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5}
    last = {"input_tokens": 0, "output_tokens": 7, "total_tokens": 7}
    _, mapper = run([text("a", usage_metadata=usage), text("b", usage_metadata=last)])
    assert (mapper.input_tokens, mapper.output_tokens) == (5, 7)


# thoughts


def test_thoughts_are_events_when_they_are_shown():
    chunks = [
        anthropic([{"type": "thinking", "thinking": "Let me ", "index": 0}]),
        anthropic([{"type": "thinking", "thinking": "think.", "index": 0}]),
        anthropic([{"type": "thinking", "thinking": "", "signature": "SIG", "index": 0}]),
        anthropic([{"type": "text", "text": "Answer", "index": 1}]),
    ]
    events, _ = run(chunks, show_thoughts=True)
    assert kinds(events) == [
        "STEP_STARTED",
        "REASONING_START",
        "REASONING_MESSAGE_START",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_MESSAGE_CONTENT",
        "REASONING_ENCRYPTED_VALUE",
        "REASONING_MESSAGE_END",
        "REASONING_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STEP_FINISHED",
    ]
    assert "".join(e.delta for e in events if e.type.value == "REASONING_MESSAGE_CONTENT") == "Let me think."
    encrypted = next(e for e in events if e.type.value == "REASONING_ENCRYPTED_VALUE")
    assert encrypted.encrypted_value == "SIG" and encrypted.subtype == "message"
    assert encrypted.entity_id == events[1].message_id


def test_thoughts_are_dropped_when_they_are_not_shown():
    chunks = [
        anthropic([{"type": "thinking", "thinking": "secret", "index": 0}]),
        anthropic([{"type": "text", "text": "Answer", "index": 1}]),
    ]
    events, _ = run(chunks, show_thoughts=False)
    assert not [k for k in kinds(events) if k.startswith("REASONING")]
    assert "secret" not in " ".join(e.model_dump_json() for e in events)


def test_an_openai_reasoning_summary_keeps_its_id():
    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Weighing", "index": 0}]}
        ],
        id="a",
        response_metadata={"model_provider": "openai"},
    )
    events, _ = run([chunk], show_thoughts=True)
    assert events[1].message_id == "rs_1" and events[3].delta == "Weighing"


def test_a_google_thought_is_reasoning():
    chunk = AIMessageChunk(
        content=[{"type": "thinking", "thinking": "hmm"}], id="a", response_metadata={"model_provider": "google_genai"}
    )
    events, _ = run([chunk], show_thoughts=True)
    assert "REASONING_MESSAGE_CONTENT" in kinds(events)


def test_a_second_thought_block_is_a_second_reasoning_message():
    chunks = [
        anthropic([{"type": "thinking", "thinking": "one", "index": 0}]),
        anthropic([{"type": "thinking", "thinking": "two", "index": 2}]),
    ]
    events, _ = run(chunks, show_thoughts=True)
    assert kinds(events).count("REASONING_START") == 2


# tool calls


def test_a_tool_call_streams_its_arguments_and_its_result():
    first = call("weather", '{"city"', "c1")
    more = AIMessageChunk(
        content="", id="a", tool_call_chunks=[{"name": None, "args": ': "Paris"}', "id": None, "index": 0}]
    )
    result = ToolMessage(content="sunny in Paris", tool_call_id="c1", id="tm1")
    events, _ = run([first, more, result, text("It is sunny.", turn="b")])

    assert kinds(events) == [
        "STEP_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "STEP_FINISHED",
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STEP_FINISHED",
    ]
    start, result_event = events[1], events[5]
    assert (start.tool_call_id, start.tool_call_name) == ("c1", "weather")
    assert "".join(e.delta for e in events if e.type.value == "TOOL_CALL_ARGS") == '{"city": "Paris"}'
    assert (result_event.tool_call_id, result_event.content, result_event.message_id) == ("c1", "sunny in Paris", "tm1")
    assert start.parent_message_id  # the assistant message of the turn


def test_text_is_closed_before_a_tool_call_opens():
    events, _ = run([text("Let me look. "), call("weather", "{}", "c1")])
    assert kinds(events) == [
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "STEP_FINISHED",
    ]
    assert events[1].message_id == events[4].parent_message_id  # the call belongs to the message that has the text


def test_two_tool_calls_in_one_turn():
    chunks = [
        call("a", '{"x": 1}', "c1", index=0),
        call("b", '{"y": 2}', "c2", index=1),
        ToolMessage(content="one", tool_call_id="c1"),
        ToolMessage(content="two", tool_call_id="c2"),
    ]
    events, _ = run(chunks)
    assert kinds(events).count("TOOL_CALL_START") == 2 and kinds(events).count("TOOL_CALL_RESULT") == 2
    assert [e.content for e in events if e.type.value == "TOOL_CALL_RESULT"] == ["one", "two"]


def test_a_tool_result_in_blocks_is_text():
    result = ToolMessage(content=[{"type": "text", "text": "ab"}, {"type": "text", "text": "cd"}], tool_call_id="c1")
    events, _ = run([call("x", "{}", "c1"), result])
    assert next(e for e in events if e.type.value == "TOOL_CALL_RESULT").content == "abcd"


def test_the_end_closes_everything_that_is_open():
    mapper = EventMapper(show_thoughts=True)
    events = mapper.feed(text("half a sent"))
    events += mapper.feed(call("x", '{"a"', "c1"))
    events += mapper.finish()
    assert_valid([RunStartedEvent(thread_id="t", run_id="r"), *events, RunFinishedEvent(thread_id="t", run_id="r")])
    assert kinds(events)[-2:] == ["TOOL_CALL_END", "STEP_FINISHED"]


def test_a_run_that_streamed_nothing_has_no_events():
    assert EventMapper().finish() == []
