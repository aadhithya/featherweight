"""Errors of the library that an application meets by name: a run that cannot work as it is set up, and a request that cannot be taken."""


class SetupError(Exception):
    """The run is set up in a way that cannot work: no API key, an MCP server that cannot start, two tools with one
    name. The runner does not retry it."""


class BadRequest(ValueError):
    """A client's request is not valid. A web framework can answer it with a 4xx (422): the message says what is wrong."""


class AttachmentLimitError(BadRequest):
    """A conversation has more attachments, or more bytes of them, than the streaming agent allows (`max_attachments`,
    `max_bytes`). It is a `BadRequest`, so a handler for that answers it; a handler for this class can answer 413 (Payload
    Too Large) and leave 422 for the other bad requests. It is raised by `runner.stream()` before any run exists."""


# 4xx codes that can succeed on a retry; every other 4xx means the request itself is wrong.
RETRYABLE_4XX = {408, 409, 429}
