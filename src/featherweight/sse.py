"""Server-sent events: the one way Featherweight streams job events to a client.

`encode` turns the stream of a job's events (`job_events`) into SSE text, and `HEADERS` are the headers of the response. There is
no server here: your API sends the response (a `StreamingResponse` in FastAPI). `id` is the sequence number of the event, so a
browser that reconnects sends `Last-Event-ID` and the stream resumes after it.

    @app.get("/jobs/{job_id}/events")
    async def events(job_id: str, request: Request):
        after = int(request.headers.get("last-event-id", 0))
        return StreamingResponse(ftw.sse.encode(ftw.job_events(db, job_id, after)), headers=ftw.sse.HEADERS)
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator

from featherweight.db import JobEvent

HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
KEEP_ALIVE = ": keep-alive\n\n"


def format_event(event: JobEvent) -> str:
    """One event as SSE: `id`, `event` (its type) and `data` (JSON, compact)."""
    data = {
        "seq": event.seq,
        "job_id": event.job_id,
        "type": event.type,
        "status": event.status.value,
        "error_type": event.error_type,
        "error_hash": event.error_hash,
        "at": event.at.isoformat().replace("+00:00", "Z"),
    }
    return f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def encode(events: AsyncIterator[JobEvent], *, keep_alive: float = 15.0) -> AsyncGenerator[str, None]:
    """The events as SSE text. A comment line goes out every `keep_alive` seconds while nothing arrives, to keep the connection
    open. The stream ends when the events end, and closes the source when the consumer stops."""
    pending: asyncio.Future[JobEvent] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(events))
            done, _ = await asyncio.wait({pending}, timeout=keep_alive)
            if not done:
                yield KEEP_ALIVE
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                pending = None
                return
            pending = None
            yield format_event(event)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()
