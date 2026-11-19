"""The error of an adapter that is given a request it cannot read."""


class BadRequest(ValueError):
    """A client's request is not valid. A web framework can answer it with a 4xx (422): the message says what is wrong."""
