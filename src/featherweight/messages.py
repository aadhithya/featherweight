"""Conversion between the messages of a client (AG-UI) and the messages of the model (LangChain).

The wire format is AG-UI's, the storage format is LangChain's: its chat message histories and stores read and write
LangChain messages, and they hold what the model needs on the next request (tool call ids, reasoning with its signature).
`to_langchain` and `to_agui` keep those exactly.
"""

import base64
import binascii
import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from ag_ui.core import (
    AssistantMessage,
    AudioInputContent,
    DeveloperMessage,
    DocumentInputContent,
    FunctionCall,
    ImageInputContent,
    InputContentDataSource,
    Message,
    ReasoningMessage,
    SystemMessage,
    TextInputContent,
    ToolCall,
    UserMessage,
    VideoInputContent,
)
from ag_ui.core import ToolMessage as AgUiToolMessage
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages import SystemMessage as LcSystemMessage

DataSource = InputContentDataSource
TextPart = TextInputContent
_PART_CLASSES: dict[str, Any] = {
    "image": ImageInputContent,
    "audio": AudioInputContent,
    "video": VideoInputContent,
    "document": DocumentInputContent,
}


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


# What a user message may carry besides text: the AG-UI part, and the LangChain block it becomes.
_MEDIA_BLOCKS = {"image": "image", "audio": "audio", "video": "video", "document": "file"}
_MEDIA_PARTS = {block: part for part, block in _MEDIA_BLOCKS.items()}


_CHUNK = 64 * 1024  # characters checked at a time: a multiple of 4, so that every chunk is whole groups of base64
_WHITESPACE = re.compile(r"\s")
_URL_SAFE = re.compile(r"[-_]")
_ALPHABET = re.compile(r"[A-Za-z0-9+/]*={0,2}")


def _why_not_base64(value: str, error: Exception) -> str:
    """The likely cause of data that is not base64, for the person who sent it. Never quotes the data."""
    if value.startswith("data:"):
        return "it is a data: URL. Send only the part after the comma"
    if _WHITESPACE.search(value):
        return "it has line breaks or spaces. Send it in one line (base64.b64encode gives that; base64.encodebytes does not)"
    if _URL_SAFE.search(value):
        return "it uses the URL-safe alphabet ('-' or '_'). Send the standard one ('+' and '/')"
    if len(value) % 4 and _ALPHABET.fullmatch(value):
        return "it is not padded: its length must be a multiple of 4 (add '=')"
    if "=" in value.rstrip("="):
        return "it has '=' padding before its end: padding may only end the data"
    if not _ALPHABET.fullmatch(value):
        return "it has characters outside the base64 alphabet (A-Z, a-z, 0-9, '+' and '/', padded with '=')"
    return str(error)


def _check_base64(value: str) -> None:
    """Raises `binascii.Error` unless `value` is what `base64.b64decode(value, validate=True)` accepts. It decodes one chunk at a
    time and drops it, so no copy of the whole attachment is made. Padding may end the last chunk only."""
    for start in range(0, len(value), _CHUNK):
        chunk = value[start : start + _CHUNK]
        if start + _CHUNK < len(value) and "=" in chunk:
            raise binascii.Error("Excess data after padding")
        base64.b64decode(chunk, validate=True)


def _media_block(part: Any, where: str) -> dict[str, Any]:
    """A media part of a user message as a LangChain content block. Only inline data is accepted: featherweight fetches
    nothing, so an application that wants a URL downloads it and sends the bytes."""
    kind = part.type
    source: Any = getattr(part, "source", None)
    if getattr(source, "type", None) != "data":
        raise ValueError(
            f"{where} is {kind} content that is not inline data ({getattr(source, 'type', 'no source')} source): "
            "download the file and send its bytes as a data source"
        )
    if not source.mime_type:
        raise ValueError(f"{where} is {kind} content without a mime type")
    if not source.value:
        raise ValueError(
            f"{where} is {kind} content with no data: an empty attachment is refused, send a file that has bytes"
        )
    try:
        _check_base64(source.value)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"{where} is {kind} content whose data is not base64: {_why_not_base64(source.value, exc)}"
        ) from exc
    block: dict[str, Any] = {"type": _MEDIA_BLOCKS[kind], "base64": source.value, "mime_type": source.mime_type}
    filename = (part.metadata or {}).get("filename") if isinstance(part.metadata, dict) else None
    if kind == "document" and filename:
        block["filename"] = str(filename)  # OpenAI wants a name for an inline file
    return block


def _user_content(content: Any, where: str) -> str | list[str | dict[Any, Any]]:
    """The content of a user message: text stays a string, and a message with attachments becomes a list of blocks in
    the order of its parts."""
    if content is None or isinstance(content, str):
        return content or ""
    blocks: list[dict[str, Any]] = []
    for index, part in enumerate(content):
        kind = getattr(part, "type", None)
        if kind == "text":
            blocks.append({"type": "text", "text": part.text})
        elif kind in _MEDIA_BLOCKS:
            blocks.append(_media_block(part, f"{where}.content[{index}]"))
        else:
            raise ValueError(f"{where}.content[{index}] is a {kind or '?'} part, which is not supported")
    if all(block["type"] == "text" for block in blocks):
        return "".join(block["text"] for block in blocks)
    return [*blocks]


def decoded_size(value: str) -> int:
    """The size in bytes of base64 text once decoded, from its length and its padding: nothing is decoded."""
    padding = len(value) - len(value.rstrip("="))
    return max(len(value) * 3 // 4 - min(padding, 2), 0)


def count_attachments(messages: Sequence[Message]) -> tuple[int, int]:
    """The number of media parts (image, document, audio, video) of all the messages, and the decoded size in bytes of
    their data sources. Every turn sends the whole history, so the attachments of earlier messages count."""
    count = size = 0
    for message in messages:
        content = getattr(message, "content", None)
        if message.role != "user" or content is None or isinstance(content, str):
            continue
        for part in content:
            if getattr(part, "type", None) in _MEDIA_BLOCKS:
                count += 1
                value = getattr(getattr(part, "source", None), "value", None)
                if isinstance(value, str):
                    size += decoded_size(value)
    return count, size


def to_langchain(messages: Sequence[Message]) -> list[BaseMessage]:
    """A client's history as LangChain messages.

    A reasoning message (a thought, with its signature) is attached to the assistant message that follows it, as a
    `reasoning` content block, which is how LangChain carries it. An activity message is a UI element, not part of the
    conversation, and is left out. A user message may carry images, documents, audio and video as inline data: they become
    LangChain content blocks, in order. Raises `ValueError` for an attachment that is not inline data (a URL), is not base64
    or has no mime type, for content that is not text in any other message, and for tool call arguments that are not JSON."""
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
            converted.append(HumanMessage(content=_user_content(message.content, where), id=message.id))
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
            converted.append(UserMessage(id=message_id, content=_user_parts(message)))
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


def _user_parts(message: HumanMessage) -> str | list[Any]:
    """The content of a user message for a client: its text, or its parts (text and attachments) when it has attachments."""
    if isinstance(message.content, str) or not any(
        isinstance(block, dict) and block.get("type") != "text" for block in message.content
    ):
        return message.text
    parts: list[Any] = []
    blocks: list[dict[str, Any]] = [dict(block) for block in message.content_blocks]
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            parts.append(TextPart(text=block["text"]))
        elif kind in _MEDIA_PARTS and block.get("base64") and block.get("mime_type"):
            source = DataSource(value=block["base64"], mime_type=block["mime_type"])
            metadata = {"filename": block["filename"]} if block.get("filename") else None
            parts.append(_PART_CLASSES[_MEDIA_PARTS[kind]](source=source, metadata=metadata))
        else:
            raise ValueError(
                f"A user message has a {kind} block that is not inline data: only text and attachments with their "
                "bytes (base64 and a mime type) can be sent to a client"
            )
    return parts


def _signature(block: Any) -> str | None:
    extras = block.get("extras") or {}
    for key in ("signature", "encrypted_content"):
        if extras.get(key):
            return str(extras[key])
    return None
