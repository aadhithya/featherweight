"""A file attached to a chat message, with the real model: it reads a random code that is only in a small PDF."""

import base64
import secrets

from ag_ui.core import DocumentInputContent, InputContentDataSource, TextInputContent, UserMessage

from featherweight import ChatInput, StreamingAgent

CODE = f"DOC-{secrets.token_hex(3).upper()}"


def pdf_with(text: str) -> bytes:
    """A one-page PDF that shows `text`."""
    stream = f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


class Reader(StreamingAgent):
    name: str = "reader"
    description: str = "Answers questions about an attached file"
    version: str = "0.1.0"
    prompt: str = "Answer from the attached file, in a sentence. Quote codes exactly."


async def test_a_chat_reads_a_pdf_that_is_attached(runner, provider):
    source = InputContentDataSource(
        value=base64.b64encode(pdf_with(f"The access code is {CODE}")).decode(), mime_type="application/pdf"
    )
    parts = [
        TextInputContent(text="What is the access code in the file?"),
        DocumentInputContent(source=source, metadata={"filename": "code.pdf"}),
    ]
    run = runner.stream(provider.on(Reader, "off"), ChatInput(messages=[UserMessage(id="1", content=parts)]))
    text, errors = "", []
    async for event in run:
        if event.type.value == "TEXT_MESSAGE_CONTENT":
            text += event.delta
        elif event.type.value == "RUN_ERROR":
            errors.append(event.message)

    assert not errors, errors
    print(f"\n[answer] {text!r}")
    assert CODE in text, text
