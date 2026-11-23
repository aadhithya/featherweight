"""Attachments through the adapters and the run log: a request with one is read, its bytes stay out of the events, the log and
the observers' records. Fake models only, and no network."""

import base64
import json

import pytest
from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from fakes import Streams, runner_with
from langchain_core.messages import HumanMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_streaming import Chat, collect

from featherweight.adapters import BadRequest, agui, vercel
from featherweight.messages import to_langchain
from featherweight.otel import OTelObserver
from featherweight.streaming import ChatInput
from featherweight.testing import RecordingContentObserver, RecordingObserver

PNG_BYTES = b"\x89PNG secret image bytes 12345"
PDF_BYTES = b"%PDF-1.7 secret pdf bytes 67890"
PNG = base64.b64encode(PNG_BYTES).decode()
PDF = base64.b64encode(PDF_BYTES).decode()


def agui_body(*parts) -> dict:
    """A request as a real AG-UI client sends it: camelCase names, and the parts as JSON."""
    return {
        "threadId": "t-1",
        "runId": "r-1",
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "messages": [{"id": "u1", "role": "user", "content": list(parts)}],
    }


IMAGE = {"type": "image", "source": {"type": "data", "value": PNG, "mimeType": "image/png"}}
DOCUMENT = {
    "type": "document",
    "source": {"type": "data", "value": PDF, "mimeType": "application/pdf"},
    "metadata": {"filename": "report.pdf"},
}


def blocks(chat_input: ChatInput) -> list:
    [message] = to_langchain(chat_input.messages)
    assert isinstance(message, HumanMessage)
    return list(message.content)


# AG-UI: the request


def test_an_agui_request_with_an_image_and_a_document_is_read_end_to_end():
    body = agui_body({"type": "text", "text": "what are these?"}, IMAGE, DOCUMENT)
    expected = [
        {"type": "text", "text": "what are these?"},
        {"type": "image", "base64": PNG, "mime_type": "image/png"},
        {"type": "file", "base64": PDF, "mime_type": "application/pdf", "filename": "report.pdf"},
    ]
    for chat_input in (
        agui.parse_request(body),
        agui.parse_request(json.dumps(body).encode()),
        ChatInput.from_agui(RunAgentInput.model_validate(body)),
    ):
        assert chat_input.thread_id == "t-1"
        assert blocks(chat_input) == expected


def test_an_agui_request_with_a_url_source_is_refused_as_a_bad_request():
    url = {"type": "image", "source": {"type": "url", "value": "https://example.com/a.png", "mimeType": "image/png"}}
    with pytest.raises(BadRequest):
        agui.parse_request(agui_body(url))


def test_a_data_source_without_a_mime_type_is_a_bad_request():
    with pytest.raises(BadRequest):
        agui.parse_request(agui_body({"type": "image", "source": {"type": "data", "value": PNG}}))


# Vercel: the request


def file_part(url, media: str = "image/png", **extra) -> dict:
    return {"type": "file", "mediaType": media, "url": url, **extra}


def vercel_body(*parts) -> dict:
    return {"id": "chat-1", "messages": [{"id": "u1", "role": "user", "parts": list(parts)}]}


def test_a_vercel_message_with_file_parts_becomes_media_parts_in_order():
    body = vercel_body(
        {"type": "text", "text": "what are these?"},
        file_part(f"data:image/png;base64,{PNG}"),
        file_part(f"data:application/pdf;base64,{PDF}", "application/pdf", filename="report.pdf"),
    )
    chat_input = vercel.parse_request(body)
    assert chat_input.thread_id == "chat-1"
    assert blocks(chat_input) == [
        {"type": "text", "text": "what are these?"},
        {"type": "image", "base64": PNG, "mime_type": "image/png"},
        {"type": "file", "base64": PDF, "mime_type": "application/pdf", "filename": "report.pdf"},
    ]


def test_vercel_audio_and_video_files_map_to_their_parts():
    body = vercel_body(
        file_part("data:audio/wav;base64,AAAA", "audio/wav"), file_part("data:video/mp4;base64,AAAA", "video/mp4")
    )
    assert [b["type"] for b in blocks(vercel.parse_request(body))] == ["audio", "video"]


def test_a_vercel_message_with_only_a_file_is_kept():
    [block] = blocks(vercel.parse_request(vercel_body(file_part(f"data:image/png;base64,{PNG}"))))
    assert block["type"] == "image"


def test_a_vercel_message_without_files_is_still_its_text():
    chat_input = vercel.parse_request(vercel_body({"type": "text", "text": "a"}, {"type": "text", "text": "b"}))
    assert chat_input.messages[0].content == "ab"


@pytest.mark.parametrize(
    "url", ["https://example.com/a.png", "blob:https://x/1", "data:image/png,not-base64", "", None, 5]
)
def test_a_vercel_file_that_is_not_base64_data_is_refused(url):
    with pytest.raises(BadRequest):
        vercel.parse_request(vercel_body(file_part(url)))


def test_a_vercel_file_with_bad_base64_is_refused():
    with pytest.raises(BadRequest):
        vercel.parse_request(vercel_body(file_part("data:image/png;base64,@@@not base64@@@")))


# the events: an attachment turn streams like any other, and the events carry no bytes


def attachment_input() -> ChatInput:
    return agui.parse_request(agui_body({"type": "text", "text": "describe"}, IMAGE, DOCUMENT))


async def test_an_attachment_turn_streams_the_same_events_through_both_adapters(db):
    model = Streams(turns=[["It is ", "a cat"]])
    run = runner_with(db, model).stream(Chat, attachment_input())
    events = await collect(run)
    assert [e.type.value for e in events] == [
        "RUN_STARTED",
        "STEP_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "STEP_FINISHED",
        "RUN_FINISHED",
    ]
    assert PNG in json.dumps([m.content for m in model.seen[0]])  # the model did get it
    translator = vercel.Translator()
    parts = [part for e in events for part in translator.feed(e)]
    assert [p["type"] for p in parts] == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
    ]
    wire = "".join(EventEncoder().encode(e) for e in events) + json.dumps(parts)
    assert PNG not in wire and PDF not in wire and "report.pdf" not in wire


async def test_the_agui_stream_of_an_attachment_turn_has_no_bytes(db):
    run = runner_with(db, Streams(turns=[["ok"]])).stream(Chat, attachment_input())
    chunks = [chunk async for chunk in agui.encode(run)]
    assert chunks and not any(PNG in c or PDF in c for c in chunks)


async def test_a_vercel_attachment_turn_encodes_without_the_bytes(db):
    body = vercel_body({"type": "text", "text": "describe"}, file_part(f"data:image/png;base64,{PNG}"))
    run = runner_with(db, Streams(turns=[["ok"]])).stream(Chat, vercel.parse_request(body))
    wire = "".join([c async for c in vercel.encode(run)])
    assert wire.endswith(vercel.DONE) and PNG not in wire


# the run log and the observers


async def test_the_run_log_holds_no_byte_of_the_attachment(db):
    run = runner_with(db, Streams(turns=[["It is a cat"]])).stream(Chat, attachment_input())
    await collect(run)

    row = await db.get_attempt(run.attempt_id)
    assert row.input_hash  # the hash is there, the content is not
    dumped = ""
    async with db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cursor:
        tables = [r[0] for r in await cursor.fetchall()]
    assert "attempts" in tables
    for table in tables:
        async with db.conn.execute(f'SELECT * FROM "{table}"') as cursor:  # noqa: S608  # our own table names
            dumped += " ".join(str(tuple(r)) for r in await cursor.fetchall())
    for secret in (PNG, PDF, PNG_BYTES.decode("latin-1"), "report.pdf", "secret"):
        assert secret not in dumped


async def test_the_observers_records_hold_no_attachment_by_default(db):
    seen = RecordingObserver()
    run = runner_with(db, Streams(turns=[["ok"]]), observers=[seen]).stream(Chat, attachment_input())
    await collect(run)

    assert "model_call_started" in seen.names()
    recorded = repr(seen.events)
    assert PNG not in recorded and PDF not in recorded and "report.pdf" not in recorded


async def test_an_observer_that_asks_for_content_gets_the_messages(db):
    seen = RecordingContentObserver()
    run = runner_with(db, Streams(turns=[["ok"]]), observers=[seen]).stream(Chat, attachment_input())
    await collect(run)
    [start] = seen.records("model_call_started")
    assert start.messages is not None  # the opt-in: an observer that asks for content is given what the model saw


async def test_the_spans_hold_no_attachment_unless_content_is_asked_for(db):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observer = OTelObserver(capture_content=False, tracer_provider=provider)
    run = runner_with(db, Streams(turns=[["ok"]]), observers=[observer]).stream(Chat, attachment_input())
    await collect(run)

    spans = exporter.get_finished_spans()
    assert spans
    recorded = repr([dict(span.attributes or {}) for span in spans])
    assert PNG not in recorded and PDF not in recorded and "report.pdf" not in recorded
