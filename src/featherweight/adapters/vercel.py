"""The Vercel AI SDK adapter: the UI message stream, for `useChat` and the other clients of the AI SDK.

It reads a request into a `ChatInput`, and translates the AG-UI events of a `StreamingRun` into the parts of the UI message
stream. It is not a server: it gives you the strings, you send them with any web framework.

    chat_input = parse_request(await request.json())
    run = runner.stream(Support, chat_input)
    return StreamingResponse(encode(run), headers=HEADERS)

Written against the protocol as documented at ai-sdk.dev (checked 2026-11-19): see docs/streaming.md.
"""

import json
import logging
from collections.abc import AsyncGenerator, Iterable
from typing import Any

from ag_ui.core import (
    AssistantMessage,
    BaseEvent,
    FunctionCall,
    Message,
    ReasoningMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from pydantic import ValidationError

from featherweight.adapters.errors import BadRequest
from featherweight.streaming import ChatInput, StreamingRun

logger = logging.getLogger(__name__)

CONTENT_TYPE = "text/event-stream"
HEADERS = {
    "Content-Type": CONTENT_TYPE,
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "x-vercel-ai-ui-message-stream": "v1",
    "x-accel-buffering": "no",
}
DONE = "data: [DONE]\n\n"


# the request: the messages of the client, as UIMessage, to AG-UI messages


def parse_request(body: dict[str, Any] | str | bytes | list[Any]) -> ChatInput:
    """The request of `useChat` (`{"id": ..., "messages": [UIMessage, ...]}`, or just the list of messages) as a
    `ChatInput`. `id`, the chat's id, becomes the `thread_id`. Raises `BadRequest` for a body that cannot be read or a
    history that `ChatInput` refuses (a system message, a last message that is not the user's)."""
    if isinstance(body, str | bytes):
        try:
            body = json.loads(body)
        except ValueError as exc:
            raise BadRequest(f"The body is not JSON: {exc}") from exc
    thread_id = None
    if isinstance(body, dict):
        thread_id = body.get("id") if isinstance(body.get("id"), str) else None
        body = body.get("messages")  # ty: ignore[invalid-assignment]
    if not isinstance(body, list):
        raise BadRequest("The body needs a list of messages")
    try:
        return ChatInput(messages=to_agui_messages(body), thread_id=thread_id)
    except ValidationError as exc:
        raise BadRequest("; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())) from exc


def _json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def to_agui_messages(ui_messages: list[dict[str, Any]]) -> list[Message]:
    """UIMessages as AG-UI messages. An assistant UIMessage with tool parts becomes the messages the model saw: an
    assistant message with its tool calls, then the tool results, then the next step's assistant message, and so on."""
    messages: list[Message] = []
    for index, ui in enumerate(ui_messages):
        if not isinstance(ui, dict) or not isinstance(ui.get("parts"), list):
            raise BadRequest(f"messages[{index}] needs a role and a list of parts")
        base = str(ui.get("id") or f"m{index}")
        role = ui.get("role")
        if role == "user":
            text = "".join(p.get("text", "") for p in ui["parts"] if isinstance(p, dict) and p.get("type") == "text")
            messages.append(UserMessage(id=base, content=text))
        elif role == "assistant":
            messages += _assistant(base, ui["parts"])
        elif role == "system":
            raise BadRequest(f"messages[{index}] is a system message: a client cannot send one")
        else:
            raise BadRequest(f"messages[{index}] has the role {role!r}")
    return messages


def _assistant(base: str, parts: list[dict[str, Any]]) -> list[Message]:
    """One assistant UIMessage as the messages of its steps."""
    out: list[Message] = []
    step: list[dict[str, Any]] = []

    def close() -> None:
        if not step:
            return
        text = "".join(p.get("text", "") for p in step if p.get("type") == "text")
        reasoning = "".join(p.get("text", "") for p in step if p.get("type") == "reasoning")
        calls = [p for p in step if str(p.get("type", "")).startswith("tool-") or p.get("type") == "dynamic-tool"]
        message_id = f"{base}-{len(out)}"
        if reasoning:
            out.append(ReasoningMessage(id=f"{message_id}-reasoning", content=reasoning))
        tool_calls = [
            ToolCall(id=c["toolCallId"], function=FunctionCall(name=_tool_name(c), arguments=_json(c.get("input", {}))))
            for c in calls
            if c.get("state") in ("input-available", "output-available", "output-error")
        ]
        if text or tool_calls:
            out.append(AssistantMessage(id=message_id, content=text or None, tool_calls=tool_calls or None))
        for c in calls:
            if c.get("state") == "output-available":
                out.append(
                    ToolMessage(
                        id=f"{c['toolCallId']}-result", tool_call_id=c["toolCallId"], content=_json(c.get("output"))
                    )
                )
            elif c.get("state") == "output-error":
                error = str(c.get("errorText", "error"))
                out.append(
                    ToolMessage(
                        id=f"{c['toolCallId']}-result", tool_call_id=c["toolCallId"], content=error, error=error
                    )
                )
        step.clear()

    for part in parts:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "step-start":
            close()
        elif kind in ("text", "reasoning", "dynamic-tool") or str(kind).startswith("tool-"):
            step.append(part)
        # files, sources, data-* and the rest have no counterpart in the model's conversation
    close()
    return out


def _tool_name(part: dict[str, Any]) -> str:
    return str(part["toolName"]) if part.get("type") == "dynamic-tool" else str(part["type"]).removeprefix("tool-")


# the response: AG-UI events to the parts of the UI message stream


def _sse(part: dict[str, Any]) -> str:
    return f"data: {json.dumps(part, separators=(',', ':'))}\n\n"


class Translator:
    """Turns the events of one run into the parts of the stream, one event at a time. A part is a dict; `encode` sends them
    as server-sent events. The events that have no part (state, activity, the opaque value of a thought) are dropped."""

    def __init__(self) -> None:
        self._calls: dict[str, tuple[str, list[str]]] = {}  # tool call id -> (name, pieces of its arguments)

    def feed(self, event: BaseEvent) -> list[dict[str, Any]]:
        kind = event.type.value
        e: Any = event
        if kind == "RUN_STARTED":
            return [{"type": "start", "messageId": e.run_id}]
        if kind == "STEP_STARTED":
            return [{"type": "start-step"}]
        if kind == "STEP_FINISHED":
            return [{"type": "finish-step"}]
        if kind == "TEXT_MESSAGE_START":
            return [{"type": "text-start", "id": e.message_id}]
        if kind == "TEXT_MESSAGE_CONTENT":
            return [{"type": "text-delta", "id": e.message_id, "delta": e.delta}]
        if kind == "TEXT_MESSAGE_END":
            return [{"type": "text-end", "id": e.message_id}]
        if kind == "REASONING_START":
            return [{"type": "reasoning-start", "id": e.message_id}]
        if kind == "REASONING_MESSAGE_CONTENT":
            return [{"type": "reasoning-delta", "id": e.message_id, "delta": e.delta}]
        if kind == "REASONING_END":
            return [{"type": "reasoning-end", "id": e.message_id}]
        if kind == "TOOL_CALL_START":
            self._calls[e.tool_call_id] = (e.tool_call_name, [])
            return [{"type": "tool-input-start", "toolCallId": e.tool_call_id, "toolName": e.tool_call_name}]
        if kind == "TOOL_CALL_ARGS":
            if e.tool_call_id in self._calls:
                self._calls[e.tool_call_id][1].append(e.delta)
            return [{"type": "tool-input-delta", "toolCallId": e.tool_call_id, "inputTextDelta": e.delta}]
        if kind == "TOOL_CALL_END":
            name, pieces = self._calls.pop(e.tool_call_id, ("", []))
            return [
                {
                    "type": "tool-input-available",
                    "toolCallId": e.tool_call_id,
                    "toolName": name,
                    "input": _parse("".join(pieces)),
                }
            ]
        if kind == "TOOL_CALL_RESULT":
            return [{"type": "tool-output-available", "toolCallId": e.tool_call_id, "output": _parse(e.content)}]
        if kind == "RUN_FINISHED":
            if getattr(e.outcome, "type", "success") == "cancelled":
                return [{"type": "abort", "reason": "stopped by the user"}]
            return [{"type": "finish"}]
        if kind == "RUN_ERROR":
            return [{"type": "error", "errorText": e.message}]
        return []


def _parse(text: Any) -> Any:
    """The JSON a tool's arguments or result holds, or the text itself when it is not JSON."""
    if not isinstance(text, str):
        return text
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


def translate(events: Iterable[BaseEvent]) -> list[str]:
    """The events of a whole run as server-sent events, ending with `[DONE]`. For tests and replays; use `encode` to stream."""
    translator = Translator()
    out = [_sse(part) for event in events for part in translator.feed(event)]
    return [*out, DONE]


async def encode(run: StreamingRun) -> AsyncGenerator[str, None]:
    """The server-sent events of a run, as it streams, ending with `[DONE]`. Send them with the headers in `HEADERS`.
    Stopping the iteration (a client that went away) stops the run."""
    translator = Translator()
    events = aiter(run)
    try:
        async for event in events:
            for part in translator.feed(event):
                yield _sse(part)
        yield DONE
    finally:
        close = getattr(events, "aclose", None)  # an early stop closes the run
        if close is not None:
            await close()
