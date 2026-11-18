"""A small MCP server over stdio, for the tests of MCP tools. Run as a script: `python tests/mcp_fake_server.py`."""

import asyncio
import base64
import os
import struct
import zlib

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import BlobResourceContents, EmbeddedResource, ResourceLink

server = FastMCP("fake")


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))
    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _pdf(line: str) -> bytes:
    """A one-page PDF that says `line`."""
    stream = f"BT /F1 18 Tf 20 50 Td ({line}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref)


@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@server.tool()
async def pid(delay_s: float = 0) -> str:
    """The process id of this server, after an optional delay."""
    await asyncio.sleep(delay_s)
    return str(os.getpid())


@server.tool()
def boom() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


@server.tool()
def die() -> str:
    """Kills the server process."""
    os._exit(1)


@server.tool()
def has_env(name: str) -> str:
    """Whether the server has the environment variable, and how long it is."""
    value = os.environ.get(name)
    return "unset" if value is None else f"set, {len(value)} characters"


@server.tool()
def big_text(length: int) -> str:
    """A text of the given length."""
    return "x" * length


@server.tool()
def picture() -> Image:
    """A small red picture."""
    return Image(data=_png(), format="png")


@server.tool()
def link() -> list:
    """A link to a document."""
    return [
        ResourceLink(type="resource_link", uri="https://example.invalid/a.pdf", name="a", mimeType="application/pdf")
    ]


@server.tool()
def embedded() -> list:
    """A document that is part of the result."""
    blob = base64.b64encode(b"%PDF-1.4 not a real document").decode()
    return [
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(uri="file:///note.pdf", mimeType="application/pdf", blob=blob),
        )
    ]


@server.tool()
def access_note() -> list:
    """A one-page PDF with the access code."""
    blob = base64.b64encode(_pdf("The access code is 7421")).decode()
    return [
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(uri="file:///access.pdf", mimeType="application/pdf", blob=blob),
        )
    ]


@server.tool()
def city() -> dict:
    """Structured data."""
    return {"city": "Oslo", "population": 709000}


if __name__ == "__main__":
    server.run("stdio")
