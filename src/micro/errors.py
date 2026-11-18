"""Errors that say a run cannot work as it is set up, so another attempt would fail the same way."""


class SetupError(Exception):
    """The run is set up in a way that cannot work: no API key, an MCP server that cannot start, two tools with one
    name. The runner does not retry it."""


# 4xx codes that can succeed on a retry; every other 4xx means the request itself is wrong.
RETRYABLE_4XX = {408, 409, 429}
