"""Attachments in a chat: the converters, what `ChatInput` refuses, and the round trips. Fake models only, and no network."""

import base64

import pytest
from ag_ui.core import (
    AssistantMessage,
    AudioInputContent,
    DocumentInputContent,
    ImageInputContent,
    InputContentDataSource,
    InputContentUrlSource,
    TextInputContent,
    UserMessage,
    VideoInputContent,
)
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from featherweight.messages import to_agui, to_langchain
from featherweight.streaming import ChatInput

PNG = base64.b64encode(b"\x89PNG fake image bytes").decode()
PDF = base64.b64encode(b"%PDF-1.7 fake pdf bytes").decode()
WAV = base64.b64encode(b"RIFF fake wav bytes").decode()
MP4 = base64.b64encode(b"fake mp4 bytes").decode()


def data(value: str, mime: str) -> InputContentDataSource:
    return InputContentDataSource(value=value, mime_type=mime)


def user(*parts, message_id: str = "u1") -> UserMessage:
    return UserMessage(id=message_id, content=list(parts))


# AG-UI -> LangChain


def test_an_image_becomes_an_image_block():
    [message] = to_langchain(
        [user(TextInputContent(text="what is this?"), ImageInputContent(source=data(PNG, "image/png")))]
    )
    assert isinstance(message, HumanMessage)
    assert message.content == [
        {"type": "text", "text": "what is this?"},
        {"type": "image", "base64": PNG, "mime_type": "image/png"},
    ]


def test_a_document_becomes_a_file_block_with_its_filename():
    part = DocumentInputContent(source=data(PDF, "application/pdf"), metadata={"filename": "report.pdf"})
    [message] = to_langchain([user(part)])
    assert message.content == [
        {"type": "file", "base64": PDF, "mime_type": "application/pdf", "filename": "report.pdf"}
    ]


def test_a_document_without_a_filename_gets_none_invented():
    [message] = to_langchain([user(DocumentInputContent(source=data(PDF, "application/pdf")))])
    assert "filename" not in message.content[0]


def test_audio_and_video_become_blocks():
    [message] = to_langchain(
        [user(AudioInputContent(source=data(WAV, "audio/wav")), VideoInputContent(source=data(MP4, "video/mp4")))]
    )
    assert message.content == [
        {"type": "audio", "base64": WAV, "mime_type": "audio/wav"},
        {"type": "video", "base64": MP4, "mime_type": "video/mp4"},
    ]


def test_the_parts_keep_their_order():
    parts = [
        TextInputContent(text="first"),
        ImageInputContent(source=data(PNG, "image/png")),
        TextInputContent(text="second"),
        DocumentInputContent(source=data(PDF, "application/pdf")),
    ]
    [message] = to_langchain([user(*parts)])
    assert [block["type"] for block in message.content] == ["text", "image", "text", "file"]


def test_a_message_with_only_an_attachment_is_valid():
    [message] = to_langchain([user(ImageInputContent(source=data(PNG, "image/png")))])
    assert [block["type"] for block in message.content] == ["image"]


def test_a_message_of_text_parts_is_still_a_string():
    [message] = to_langchain([user(TextInputContent(text="a"), TextInputContent(text="b"))])
    assert message.content == "ab"


# What is refused


def test_a_url_source_is_refused_with_the_way_out():
    part = ImageInputContent(source=InputContentUrlSource(value="https://example.com/cat.png", mime_type="image/png"))
    with pytest.raises(ValueError, match="send its bytes as a data source"):
        to_langchain([user(part)])


def test_data_that_is_not_base64_is_refused():
    with pytest.raises(ValueError, match="not base64"):
        to_langchain([user(ImageInputContent(source=data("not base64 !!", "image/png")))])


def test_a_missing_mime_type_is_refused():
    with pytest.raises(ValueError, match="without a mime type"):
        to_langchain([user(ImageInputContent(source=data(PNG, "")))])


def test_an_unsupported_part_is_refused():
    class Odd:
        type = "hologram"

    with pytest.raises(ValueError, match="hologram"):
        to_langchain([UserMessage.model_construct(id="u1", role="user", content=[Odd()])])


def test_an_assistant_message_stays_text_only():
    with pytest.raises(ValidationError):
        AssistantMessage(id="a1", content=[ImageInputContent(source=data(PNG, "image/png"))])


def test_chat_input_takes_attachments_and_refuses_a_url_before_any_run():
    ok = ChatInput(messages=[user(TextInputContent(text="look"), ImageInputContent(source=data(PNG, "image/png")))])
    assert ok.messages[-1].role == "user"
    bad = ImageInputContent(source=InputContentUrlSource(value="https://example.com/cat.png"))
    with pytest.raises(ValidationError, match="data source"):
        ChatInput(messages=[user(bad)])


def test_an_attachment_in_an_earlier_message_is_checked_too():
    bad = ImageInputContent(source=InputContentUrlSource(value="https://example.com/cat.png"))
    with pytest.raises(ValidationError, match=r"messages\[0\]"):
        ChatInput(messages=[user(bad), AssistantMessage(id="a1", content="?"), UserMessage(id="u2", content="and?")])


# LangChain -> AG-UI, and the round trips


def test_a_langchain_message_with_attachments_goes_back_to_parts():
    message = HumanMessage(
        content=[
            {"type": "text", "text": "read this"},
            {"type": "file", "base64": PDF, "mime_type": "application/pdf", "filename": "a.pdf"},
        ],
        id="u1",
    )
    [converted] = to_agui([message])
    assert [part.type for part in converted.content] == ["text", "document"]  # ty: ignore[not-iterable]
    document = converted.content[1]  # ty: ignore[not-subscriptable]
    assert document.source.value == PDF
    assert document.metadata == {"filename": "a.pdf"}


def test_attachments_survive_a_round_trip_both_ways():
    original = [
        user(
            TextInputContent(text="look"),
            ImageInputContent(source=data(PNG, "image/png")),
            DocumentInputContent(source=data(PDF, "application/pdf"), metadata={"filename": "r.pdf"}),
            AudioInputContent(source=data(WAV, "audio/wav")),
            VideoInputContent(source=data(MP4, "video/mp4")),
        )
    ]
    again = to_agui(to_langchain(original))
    assert again == original
    assert to_langchain(again) == to_langchain(original)


def test_a_langchain_block_that_is_not_inline_data_cannot_go_to_a_client():
    message = HumanMessage(content=[{"type": "image", "url": "https://example.com/cat.png"}], id="u1")
    with pytest.raises(ValueError, match="not inline data"):
        to_agui([message])


def test_chat_input_from_langchain_keeps_attachments():
    message = HumanMessage(content=[{"type": "image", "base64": PNG, "mime_type": "image/png"}], id="u1")
    chat = ChatInput.from_langchain([message])
    assert chat.messages[0].content[0].type == "image"  # ty: ignore[not-subscriptable]
