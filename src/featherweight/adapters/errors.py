"""The error of an adapter that is given a request it cannot read. It is defined in `featherweight.errors` (the streaming module raises
a subclass of it and cannot import this package); it is exported here too, where it always was."""

from featherweight.errors import BadRequest as BadRequest

__all__ = ["BadRequest"]
