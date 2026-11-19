"""The AG-UI adapter. The events of a streaming run already are AG-UI's, so this is only the edge with HTTP: read a
request into a `ChatInput`, and encode the events of a run as server-sent events. No server and no web framework."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from pydantic import ValidationError

from micro.adapters.errors import BadRequest
from micro.streaming import ChatInput, StreamingRun

logger = logging.getLogger(__name__)

CONTENT_TYPE = "text/event-stream"
# Headers for the response: the stream must not be cached or buffered on its way to the browser.
HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def _problems(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}" for e in exc.errors())


def parse_request(body: dict[str, Any] | bytes | str) -> ChatInput:
    """The JSON body of an AG-UI request (a `RunAgentInput`) as a `ChatInput`.

    `threadId` is kept. The client's `tools`, `state`, `context` and `forwardedProps` are ignored: the agent has its own
    tools, and a warning is logged when tools are sent. Raises `BadRequest` (a `ValueError`) for a body that is not JSON,
    is not a `RunAgentInput`, or is a conversation a `ChatInput` refuses (a `system` or `developer` message, a last
    message that is not the user's).
    """
    try:
        data = json.loads(body) if isinstance(body, bytes | str) else body
        request = RunAgentInput.model_validate(data)
    except json.JSONDecodeError as exc:
        raise BadRequest(f"The body is not JSON: {exc}") from exc
    except ValidationError as exc:
        raise BadRequest(f"The body is not an AG-UI request: {_problems(exc)}") from exc
    if request.tools:
        logger.warning("agui_tools_ignored count=%d: the agent has its own tools", len(request.tools))
    try:
        return ChatInput(messages=request.messages, thread_id=request.thread_id)
    except ValidationError as exc:
        raise BadRequest(f"The conversation is not valid: {_problems(exc)}") from exc


async def encode(run: StreamingRun) -> AsyncIterator[str]:
    """The events of `run` as server-sent events, one string each (`data: {...}` and a blank line), for the body of a
    streaming response. Leaving the loop early stops the run."""
    encoder = EventEncoder()
    events = aiter(run)
    try:
        async for event in events:
            yield encoder.encode(event)
    finally:
        close = getattr(events, "aclose", None)  # stopping the reader stops the run
        if close is not None:
            await close()
