"""A check that a list of AG-UI events follows the order the protocol requires. Used by the streaming tests."""

from ag_ui.core import BaseEvent


def assert_valid(events: list[BaseEvent]) -> None:
    kinds = [e.type.value for e in events]
    assert kinds[0] == "RUN_STARTED", kinds
    assert kinds[-1] in ("RUN_FINISHED", "RUN_ERROR"), kinds
    assert sum(k in ("RUN_FINISHED", "RUN_ERROR") for k in kinds) == 1, kinds
    assert kinds.count("RUN_STARTED") == 1, kinds

    text: set[str] = set()
    reasoning: set[str] = set()  # reasoning spans
    messages: set[str] = set()  # reasoning messages inside a span
    started_calls: set[str] = set()
    open_calls: set[str] = set()
    step: str | None = None
    for event in events[1:-1]:
        kind = event.type.value
        if kind == "TEXT_MESSAGE_START":
            assert not reasoning and not open_calls, "a text message opens while another kind is open"
            assert event.message_id not in text
            text.add(event.message_id)
        elif kind == "TEXT_MESSAGE_CONTENT":
            assert event.message_id in text and event.delta, "content of a text message that is not open"
        elif kind == "TEXT_MESSAGE_END":
            assert event.message_id in text
            text.discard(event.message_id)
        elif kind == "REASONING_START":
            assert not text and not open_calls, "reasoning opens while another kind is open"
            reasoning.add(event.message_id)
        elif kind == "REASONING_MESSAGE_START":
            assert event.message_id in reasoning
            messages.add(event.message_id)
        elif kind == "REASONING_MESSAGE_CONTENT":
            assert event.message_id in messages and event.delta
        elif kind == "REASONING_ENCRYPTED_VALUE":
            assert event.entity_id in reasoning
        elif kind == "REASONING_MESSAGE_END":
            assert event.message_id in messages
            messages.discard(event.message_id)
        elif kind == "REASONING_END":
            assert event.message_id in reasoning and event.message_id not in messages
            reasoning.discard(event.message_id)
        elif kind == "TOOL_CALL_START":
            assert not text and not reasoning, "a tool call opens while text or reasoning is open"
            assert event.tool_call_id not in started_calls
            started_calls.add(event.tool_call_id)
            open_calls.add(event.tool_call_id)
        elif kind == "TOOL_CALL_ARGS":
            assert event.tool_call_id in open_calls
        elif kind == "TOOL_CALL_END":
            assert event.tool_call_id in open_calls
            open_calls.discard(event.tool_call_id)
        elif kind == "TOOL_CALL_RESULT":
            assert event.tool_call_id in started_calls and event.tool_call_id not in open_calls
        elif kind == "STEP_STARTED":
            assert step is None, "a step starts inside a step"
            assert not text and not reasoning and not open_calls
            step = event.step_name
        elif kind == "STEP_FINISHED":
            assert step == event.step_name
            assert not text and not reasoning and not open_calls, "a step ends with something open"
            step = None
    assert not (text or reasoning or messages or open_calls), "something is still open at the end"
    if kinds[-1] == "RUN_FINISHED":
        assert step is None, "a step is still open at the end"
