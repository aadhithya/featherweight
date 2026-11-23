"""Attachments and the providers: a provider that says no, and the request each provider package would build for each block.

Fake models only, and no network: the request tests call the packages' own offline conversion on a model built with a dummy
key, and send nothing. The shapes are characterization of the installed versions: a test that fails after an upgrade says that
a package changed what it does with a block, and the spec and the provider reference are to be read again."""

import base64
import warnings

import pytest
from ag_ui.core import (
    AudioInputContent,
    DocumentInputContent,
    ImageInputContent,
    InputContentDataSource,
    RunErrorEvent,
    TextInputContent,
    UserMessage,
    VideoInputContent,
)
from fakes import FakeSettings, Streams, runner_with
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_google_genai.chat_models import _parse_chat_history
from langchain_openai import ChatOpenAI
from stream_check import assert_valid

from featherweight.messages import to_langchain
from featherweight.model_settings import ModelSettings
from featherweight.models import Status
from featherweight.streaming import ChatInput, StreamingAgent

RAW = b"fake bytes"
B64 = base64.b64encode(RAW).decode()


def source(mime: str) -> InputContentDataSource:
    return InputContentDataSource(value=B64, mime_type=mime)


def message(part) -> HumanMessage:
    [converted] = to_langchain([UserMessage(id="u1", content=[TextInputContent(text="look"), part])])
    assert isinstance(converted, HumanMessage)
    return converted


# a provider that rejects an attachment


class Rejecting(Streams):
    """A streaming model that refuses, as a provider does, a message that has an attachment block."""

    def _has_attachment(self, messages) -> bool:
        return any(
            isinstance(block, dict) and block.get("type") in {"image", "file", "audio", "video"}
            for m in messages
            if isinstance(m.content, list)
            for block in m.content
        )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        if self._has_attachment(messages):
            self.calls += 1
            raise ValueError("Error code: 400 - this model does not accept video input")
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


class Chat(StreamingAgent):
    name: str = "chat"
    description: str = "Talks to the user"
    version: str = "1"
    prompt: str = "You are a helpful chat agent."
    model: ModelSettings = FakeSettings()


async def test_a_provider_that_rejects_an_attachment_fails_the_turn_without_a_retry(db):
    model = Rejecting(turns=[["never used"]])
    runner = runner_with(db, model)
    parts = [TextInputContent(text="watch"), VideoInputContent(source=source("video/mp4"))]
    run = runner.stream(Chat, ChatInput(messages=[UserMessage(id="u1", content=parts)], thread_id="t-1"))
    events = [event async for event in run]
    assert_valid(events)

    assert events[0].type.value == "RUN_STARTED"
    assert isinstance(events[-1], RunErrorEvent) and "does not accept video input" in events[-1].message
    assert model.calls == 1  # not retried
    assert run.status is Status.FAILED and "does not accept video input" in run.error
    row = await db.get_attempt(run.attempt_id)
    assert row.status is Status.FAILED
    assert row.error_type == "ValueError"
    assert row.output_hash is None


async def test_the_same_model_answers_a_message_without_an_attachment(db):
    """The control of the test above: it is the attachment that is refused, not the model."""
    runner = runner_with(db, Rejecting(turns=[["fine"]]))
    run = runner.stream(Chat, ChatInput(messages=[UserMessage(id="u1", content="hi")], thread_id="t-1"))
    [event async for event in run]
    assert run.status is Status.SUCCESS


# the request each provider package would build

IMAGE = ImageInputContent(source=source("image/png"))
PDF = DocumentInputContent(source=source("application/pdf"), metadata={"filename": "report.pdf"})
PDF_NO_NAME = DocumentInputContent(source=source("application/pdf"))
TEXT_FILE = DocumentInputContent(source=source("text/plain"), metadata={"filename": "notes.txt"})
AUDIO = AudioInputContent(source=source("audio/wav"))
VIDEO = VideoInputContent(source=source("video/mp4"))

TEXT = {"type": "text", "text": "look"}


# Anthropic: `ChatAnthropic._get_request_payload` is the request body, built without a call.


@pytest.fixture(scope="module")
def anthropic() -> ChatAnthropic:
    return ChatAnthropic(model="claude-sonnet-4-5", api_key="dummy-key")


def anthropic_content(model: ChatAnthropic, part) -> list[dict]:
    [request_message] = model._get_request_payload([message(part)])["messages"]
    return request_message["content"]


def test_anthropic_image(anthropic):
    assert anthropic_content(anthropic, IMAGE) == [
        TEXT,
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": B64}},
    ]


def test_anthropic_pdf_is_a_document_block_and_the_filename_is_not_sent(anthropic):
    expected = [
        TEXT,
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": B64}},
    ]
    assert anthropic_content(anthropic, PDF) == expected
    assert anthropic_content(anthropic, PDF_NO_NAME) == expected


def test_anthropic_passes_a_text_file_through_as_a_document(anthropic):
    # characterization: the package does not look at the type; the API decides whether it accepts text/plain
    assert anthropic_content(anthropic, TEXT_FILE)[1] == {
        "type": "document",
        "source": {"type": "base64", "media_type": "text/plain", "data": B64},
    }


@pytest.mark.parametrize("part", [AUDIO, VIDEO], ids=["audio", "video"])
def test_anthropic_raises_on_audio_and_video_before_any_request(anthropic, part):
    # characterization: the package refuses at once, so there is no call (in a run this is a FAILED run, as above)
    with pytest.raises(ValueError, match="is not supported"):
        anthropic_content(anthropic, part)


# OpenAI: `_get_request_payload` builds the body, for Chat Completions and for the Responses API.


@pytest.fixture(scope="module")
def openai_chat() -> ChatOpenAI:
    return ChatOpenAI(model="gpt-4o", api_key="dummy-key")


@pytest.fixture(scope="module")
def openai_responses() -> ChatOpenAI:
    return ChatOpenAI(model="gpt-4o", api_key="dummy-key", use_responses_api=True)


def openai_content(model: ChatOpenAI, part, key: str) -> list[dict]:
    [request_message] = model._get_request_payload([message(part)])[key]
    return request_message["content"]


def test_openai_chat_image(openai_chat):
    assert openai_content(openai_chat, IMAGE, "messages") == [
        TEXT,
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
    ]


def test_openai_chat_pdf_with_a_filename(openai_chat):
    assert openai_content(openai_chat, PDF, "messages")[1] == {
        "type": "file",
        "file": {"file_data": f"data:application/pdf;base64,{B64}", "filename": "report.pdf"},
    }


def test_openai_chat_pdf_without_a_filename_gets_a_placeholder_and_a_warning(openai_chat):
    # the reason the spec never invents a name: the package puts in a placeholder, and warns
    with pytest.warns(UserWarning, match="filename"):
        content = openai_content(openai_chat, PDF_NO_NAME, "messages")
    assert content[1]["file"]["filename"] == "LC_AUTOGENERATED"


def test_openai_chat_passes_a_text_file_through(openai_chat):
    # characterization: not refused by the package, though Chat Completions takes PDFs only: the API says no
    assert openai_content(openai_chat, TEXT_FILE, "messages")[1] == {
        "type": "file",
        "file": {"file_data": f"data:text/plain;base64,{B64}", "filename": "notes.txt"},
    }


def test_openai_chat_audio_is_input_audio_with_the_format(openai_chat):
    assert openai_content(openai_chat, AUDIO, "messages")[1] == {
        "type": "input_audio",
        "input_audio": {"data": B64, "format": "wav"},
    }


def test_openai_chat_raises_on_video(openai_chat):
    with pytest.raises(ValueError, match="video is not supported"):
        openai_content(openai_chat, VIDEO, "messages")


def test_openai_responses_image_and_pdf(openai_responses):
    assert openai_content(openai_responses, IMAGE, "input") == [
        {"type": "input_text", "text": "look"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{B64}"},
    ]
    assert openai_content(openai_responses, PDF, "input")[1] == {
        "type": "input_file",
        "file_data": f"data:application/pdf;base64,{B64}",
        "filename": "report.pdf",
    }


def test_openai_responses_passes_a_text_file_through(openai_responses):
    assert openai_content(openai_responses, TEXT_FILE, "input")[1] == {
        "type": "input_file",
        "file_data": f"data:text/plain;base64,{B64}",
        "filename": "notes.txt",
    }


def test_openai_responses_silently_drops_audio(openai_responses):
    # characterization, and a trap: no error and no audio, so the model is asked the text alone
    assert openai_content(openai_responses, AUDIO, "input") == [{"type": "input_text", "text": "look"}]


def test_openai_responses_raises_on_video(openai_responses):
    with pytest.raises(ValueError, match="video is not supported"):
        openai_content(openai_responses, VIDEO, "input")


# Google: `_parse_chat_history` turns the messages into `Content` for the request, with the data as bytes.


def google_parts(part) -> list:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a warning here would be a surprise
        [content] = _parse_chat_history([message(part)])[1]
    assert content.role == "user"
    return list(content.parts)


@pytest.mark.parametrize(
    ("part", "mime"),
    [
        (IMAGE, "image/png"),
        (PDF, "application/pdf"),
        (PDF_NO_NAME, "application/pdf"),
        (TEXT_FILE, "text/plain"),
        (AUDIO, "audio/wav"),
        (VIDEO, "video/mp4"),
    ],
    ids=["image", "pdf", "pdf-no-name", "text-file", "audio", "video"],
)
def test_google_sends_every_block_inline_with_its_bytes_and_mime_type(part, mime):
    text, media = google_parts(part)
    assert text.text == "look"
    assert media.inline_data.data == RAW  # decoded: the package sends bytes
    assert media.inline_data.mime_type == mime
    assert media.inline_data.display_name is None  # the filename, when there is one, is not in the request
