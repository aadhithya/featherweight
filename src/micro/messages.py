"""Conversion between the messages of a client (AG-UI) and the messages of the model (LangChain).

The wire format is AG-UI's, the storage format is LangChain's: its chat message histories and stores read and write
LangChain messages, and they hold what the model needs on the next request (tool call ids, reasoning with its signature).
`to_langchain` and `to_agui` keep those exactly.
"""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from ag_ui.core import (
    AssistantMessage,
    DeveloperMessage,
    FunctionCall,
    Message,
    ReasoningMessage,
    SystemMessage,
    ToolCall,
    UserMessage,
)
from ag_ui.core import ToolMessage as AgUiToolMessage
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages import SystemMessage as LcSystemMessage


def _new_id() -> str:
    return uuid.uuid4().hex


def _text(content: Any, where: str) -> str:
    """A message's content as text. Parts that are not text (an image, a file) are refused."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    texts = []
    for part in content:
        if getattr(part, "type", None) != "text":
            raise ValueError(
                f"{where} has content that is not text ({getattr(part, 'type', '?')}): only text is supported"
            )
        texts.append(part.text)
    return "".join(texts)


def to_langchain(messages: Sequence[Message]) -> list[BaseMessage]:
    """A client's history as LangChain messages.

    A reasoning message (a thought, with its signature) is attached to the assistant message that follows it, as a
    `reasoning` content block, which is how LangChain carries it. An activity message is a UI element, not part of the
    conversation, and is left out. Raises `ValueError` for content that is not text, and for tool call arguments that
    are not JSON."""
    converted: list[BaseMessage] = []
    thoughts: list[dict[str, Any]] = []  # reasoning that waits for the assistant message it belongs to
    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        if isinstance(message, ReasoningMessage):
            block: dict[str, Any] = {"type": "reasoning", "reasoning": message.content, "id": message.id}
            if message.encrypted_value:
                block["extras"] = {"signature": message.encrypted_value}
            thoughts.append(block)
        elif isinstance(message, UserMessage):
            converted.append(HumanMessage(content=_text(message.content, where), id=message.id))
        elif isinstance(message, AssistantMessage):
            text = message.content or ""
            tool_calls: list[Any] = []
            for call in message.tool_calls or []:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{where} has a tool call whose arguments are not JSON: {exc}") from exc
                tool_calls.append({"name": call.function.name, "args": args, "id": call.id, "type": "tool_call"})
            content: str | list[str | dict[Any, Any]] = text
            if thoughts:
                content = [*thoughts, *([{"type": "text", "text": text}] if text else [])]
                thoughts = []
            converted.append(AIMessage(content=content, tool_calls=tool_calls, id=message.id))
        elif isinstance(message, AgUiToolMessage):
            body = message.error if message.error else _text(message.content, where)
            converted.append(
                ToolMessage(
                    content=body,
                    tool_call_id=message.tool_call_id,
                    status="error" if message.error else "success",
                    id=message.id,
                )
            )
        elif isinstance(message, SystemMessage | DeveloperMessage):
            converted.append(LcSystemMessage(content=message.content, id=message.id))
        # an activity message is not part of the conversation
    return converted


def to_agui(messages: Sequence[BaseMessage]) -> list[Message]:
    """LangChain messages as AG-UI messages: what a client shows, or sends back. An assistant message that holds thoughts
    becomes reasoning messages followed by the assistant message. The inverse of `to_langchain`."""
    converted: list[Message] = []
    for message in messages:
        message_id = message.id or _new_id()
        if isinstance(message, HumanMessage):
            converted.append(UserMessage(id=message_id, content=message.text))
        elif isinstance(message, AIMessage):
            for block in message.content_blocks:
                if block.get("type") == "reasoning" and (block.get("reasoning") or _signature(block)):
                    converted.append(
                        ReasoningMessage(
                            id=str(block.get("id") or _new_id()),
                            content=block.get("reasoning") or "",
                            encrypted_value=_signature(block),
                        )
                    )
            calls = [
                ToolCall(
                    id=call["id"] or _new_id(),
                    function=FunctionCall(name=call["name"], arguments=json.dumps(call["args"])),
                )
                for call in message.tool_calls
            ]
            converted.append(AssistantMessage(id=message_id, content=message.text or None, tool_calls=calls or None))
        elif isinstance(message, ToolMessage):
            failed = message.status == "error"
            converted.append(
                AgUiToolMessage(
                    id=message_id,
                    content=message.text,
                    tool_call_id=message.tool_call_id,
                    error=message.text if failed else None,
                )
            )
        elif isinstance(message, LcSystemMessage):
            converted.append(SystemMessage(id=message_id, content=message.text))
        else:
            raise ValueError(f"A {type(message).__name__} cannot be converted to an AG-UI message")
    return converted


def _signature(block: Any) -> str | None:
    extras = block.get("extras") or {}
    for key in ("signature", "encrypted_content"):
        if extras.get(key):
            return str(extras[key])
    return None
