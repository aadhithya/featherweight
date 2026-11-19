"""LangChain's stream of a model's output, as AG-UI events.

The events are the ones of the `ag-ui-protocol` package. This module only decides which come when, and in the order the
protocol requires: a thing is opened before it has content, closed before another kind opens, and everything that is
open is closed when the run ends or fails.
"""

import json
import uuid
from typing import Any

from ag_ui.core import (
    BaseEvent,
    ReasoningEncryptedValueEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
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
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

STEP_NAME = "model"  # a step is one model turn, with the tool results that follow it
_NO_TURN = object()  # before the first chunk: a chunk's id can be None, so None cannot say "no turn yet"


def _new_id() -> str:
    return uuid.uuid4().hex


def _signature(block: Any) -> str | None:
    """A provider's opaque value of a reasoning block: Claude's signature, OpenAI's encrypted content."""
    extras = block.get("extras") or {}
    for key in ("signature", "encrypted_content"):
        if extras.get(key):
            return str(extras[key])
    return None


def _text_of(content: Any) -> str:
    """What a tool message holds, as text."""
    if isinstance(content, str):
        return content
    return "".join(
        part if isinstance(part, str) else str(part.get("text", "")) for part in content if isinstance(part, str | dict)
    )


class EventMapper:
    """Turns the chunks of one run into AG-UI events, and counts the tokens.

    Feed it what `astream(..., stream_mode="messages")` yields, one chunk at a time. `show_thoughts` says whether the
    model's reasoning becomes events; when it is off, reasoning is dropped here (it is still in the messages the model
    returns, which is where it is kept).
    """

    def __init__(self, *, show_thoughts: bool = False) -> None:
        self.show_thoughts = show_thoughts
        self.input_tokens = 0
        self.output_tokens = 0
        self._turn: Any = _NO_TURN  # the id of the model turn that is streaming
        self._step_open = False
        self._turn_message_id: str | None = None  # the assistant message of the turn, which its tool calls belong to
        self._text: str | None = None  # the open text message
        self._text_used = False  # the first text message of a turn takes the turn's message id
        self._reasoning: str | None = None  # the open reasoning message
        self._block: Any = None  # the `index` of the block that is open, to tell where the next one starts
        self._calls: dict[Any, str] = {}  # tool call id by the index its chunks carry, for the calls of this turn
        self._open_calls: list[str] = []  # the tool calls that have no end yet, in the order they started
        self._partial: list[dict[str, Any]] = []  # what the turn that is streaming has written so far, in order
        self._partial_calls: dict[str, dict[str, Any]] = {}  # its tool calls by id: name and the arguments so far

    def feed(self, chunk: object) -> list[BaseEvent]:
        if isinstance(chunk, ToolMessage):
            return self._tool_result(chunk)
        if not isinstance(chunk, AIMessageChunk):
            return []
        usage = chunk.usage_metadata or {}
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        blocks = chunk.content_blocks
        if not blocks:
            # The closing chunk of a stream has no content, and its id can differ from the others': it does not start a turn.
            return []
        events: list[BaseEvent] = []
        if self._turn is _NO_TURN or chunk.id != self._turn:
            events += self._end_turn()
            self._turn = chunk.id
            self._turn_message_id = _new_id()
            self._step_open = True
            events.append(StepStartedEvent(step_name=STEP_NAME))
        for block in blocks:
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                events += self._write_text(block)
            elif kind == "reasoning" and self.show_thoughts:
                events += self._write_reasoning(block)
            elif kind == "tool_call_chunk":
                events += self._write_tool_call(block)
        return events

    def complete_turn(self) -> None:
        """The model turn that was streaming has finished as a whole message (the node returned it): what was kept of it
        as it streamed is not needed any more."""
        self._partial = []
        self._partial_calls = {}

    def partial_message(self) -> AIMessage | None:
        """The message of a model turn that did not finish, as far as it is valid to send back to a provider, or None.

        Text is kept. A thought is kept only with its signature (a provider rejects one without). A tool call is kept only
        when its arguments are complete JSON; one that was still streaming is dropped."""
        content: list[str | dict[Any, Any]] = []
        for part in self._partial:
            if part["kind"] == "text" and part["text"]:
                content.append({"type": "text", "text": part["text"]})
            elif part["kind"] == "reasoning" and part.get("signature"):
                content.append(
                    {
                        "type": "reasoning",
                        "reasoning": part["text"],
                        "id": part["id"],
                        "extras": {"signature": part["signature"]},
                    }
                )
        tool_calls: list[Any] = []
        for call_id, call in self._partial_calls.items():
            try:
                args = json.loads(call["args"] or "{}")
            except ValueError:
                continue
            if isinstance(args, dict):
                tool_calls.append({"name": call["name"], "args": args, "id": call_id, "type": "tool_call"})
        if not content and not tool_calls:
            return None
        first = content[0] if len(content) == 1 else None
        only_text = isinstance(first, dict) and first["type"] == "text"
        return AIMessage(
            content=first["text"] if isinstance(first, dict) and only_text else content,
            tool_calls=tool_calls,
            id=self._turn_message_id,
        )

    def finish(self) -> list[BaseEvent]:
        """Close what is open. Called at the end of a run, and before an error."""
        events = self._end_turn()
        self._turn = _NO_TURN
        return events

    # text and reasoning

    def _write_text(self, block: Any) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        if self._text is None or block.get("index") != self._block:
            events += self._close_blocks()
            message_id = self._turn_message_id if not self._text_used and self._turn_message_id else _new_id()
            self._text, self._text_used, self._block = message_id, True, block.get("index")
            events.append(TextMessageStartEvent(message_id=message_id, role="assistant"))
        events.append(TextMessageContentEvent(message_id=self._text, delta=block["text"]))
        last = self._partial[-1] if self._partial else None
        if last and last["kind"] == "text" and last["block"] == block.get("index"):
            last["text"] += block["text"]
        else:
            self._partial.append({"kind": "text", "block": block.get("index"), "text": block["text"]})
        return events

    def _write_reasoning(self, block: Any) -> list[BaseEvent]:
        text = block.get("reasoning") or ""
        signature = _signature(block)
        if not text and not signature:
            return []
        events: list[BaseEvent] = []
        if self._reasoning is None or block.get("index") != self._block:
            events += self._close_blocks()
            message_id = block.get("id") or _new_id()
            self._reasoning, self._block = message_id, block.get("index")
            events.append(ReasoningStartEvent(message_id=message_id))
            events.append(ReasoningMessageStartEvent(message_id=message_id, role="reasoning"))
        thought: dict[str, Any] = self._partial[-1] if self._partial else {}
        if not (thought.get("kind") == "reasoning" and thought.get("block") == block.get("index")):
            thought = {
                "kind": "reasoning",
                "block": block.get("index"),
                "id": self._reasoning,
                "text": "",
                "signature": None,
            }
            self._partial.append(thought)
        thought["text"] = (thought["text"] or "") + text
        thought["signature"] = signature or thought["signature"]
        if text:
            events.append(ReasoningMessageContentEvent(message_id=self._reasoning, delta=text))
        if signature:
            events.append(
                ReasoningEncryptedValueEvent(subtype="message", entity_id=self._reasoning, encrypted_value=signature)
            )
        return events

    def _close_blocks(self) -> list[BaseEvent]:
        """Close the open text or reasoning message: the next thing is of another kind, or another block."""
        events: list[BaseEvent] = []
        if self._text is not None:
            events.append(TextMessageEndEvent(message_id=self._text))
        if self._reasoning is not None:
            events.append(ReasoningMessageEndEvent(message_id=self._reasoning))
            events.append(ReasoningEndEvent(message_id=self._reasoning))
        self._text = self._reasoning = None
        self._block = None
        return events

    # tool calls

    def _write_tool_call(self, block: Any) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        index = block.get("index")
        call_id = block.get("id")
        if call_id and index not in self._calls:  # the first chunk of a call has its id and name
            events += self._close_blocks()
            self._calls[index] = call_id
            self._open_calls.append(call_id)
            self._partial_calls[call_id] = {"name": block.get("name") or "", "args": ""}
            events.append(
                ToolCallStartEvent(
                    tool_call_id=call_id,
                    tool_call_name=block.get("name") or "",
                    parent_message_id=self._turn_message_id,
                )
            )
        known = self._calls.get(index)
        if known is not None and block.get("args"):
            self._partial_calls[known]["args"] += block["args"]
            events.append(ToolCallArgsEvent(tool_call_id=known, delta=block["args"]))
        return events

    def _tool_result(self, message: ToolMessage) -> list[BaseEvent]:
        events = self._close_blocks() + self._close_calls()
        events.append(
            ToolCallResultEvent(
                message_id=message.id or _new_id(),
                tool_call_id=message.tool_call_id,
                content=_text_of(message.content),
                role="tool",
            )
        )
        return events

    def _close_calls(self) -> list[BaseEvent]:
        events: list[BaseEvent] = [ToolCallEndEvent(tool_call_id=call_id) for call_id in self._open_calls]
        self._open_calls = []
        return events

    def _end_turn(self) -> list[BaseEvent]:
        """The model turn is over: close what is open, and the step."""
        events = self._close_blocks() + self._close_calls()
        if self._step_open:
            events.append(StepFinishedEvent(step_name=STEP_NAME))
        self._step_open = False
        self._calls = {}
        self._text_used = False
        self.complete_turn()
        return events
