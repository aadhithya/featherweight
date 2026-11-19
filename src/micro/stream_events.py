"""LangChain's stream of a model's output, as AG-UI events."""

import uuid

from ag_ui.core import BaseEvent, TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent
from langchain_core.messages import AIMessageChunk


def _text(chunk: AIMessageChunk) -> str:
    content = chunk.content
    if isinstance(content, str):
        return content
    return "".join(
        block if isinstance(block, str) else block.get("text", "")
        for block in content
        if isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text")
    )


class EventMapper:
    """Turns the chunks of one run into AG-UI events, in the order the protocol requires, and counts the tokens.

    Text only for now: it opens a text message at the first text of a model turn and closes it when the turn changes
    or the run ends."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self._chunk_id: str | None = None  # the model turn the open message belongs to
        self._message_id: str | None = None  # the open text message

    def feed(self, chunk: object) -> list[BaseEvent]:
        if not isinstance(chunk, AIMessageChunk):
            return []
        usage = chunk.usage_metadata or {}
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        text = _text(chunk)
        if not text:
            return []
        events: list[BaseEvent] = []
        if self._message_id is not None and chunk.id != self._chunk_id:
            events += self.finish()
        if self._message_id is None:
            self._message_id = uuid.uuid4().hex
            self._chunk_id = chunk.id
            events.append(TextMessageStartEvent(message_id=self._message_id, role="assistant"))
        events.append(TextMessageContentEvent(message_id=self._message_id, delta=text))
        return events

    def finish(self) -> list[BaseEvent]:
        """Close what is open. Called at the end of a run, and before an error."""
        if self._message_id is None:
            return []
        event = TextMessageEndEvent(message_id=self._message_id)
        self._message_id = self._chunk_id = None
        return [event]
