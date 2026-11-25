"""The error of an adapter that is given a request it cannot read, and how it words what is wrong. `BadRequest` is defined in
`featherweight.errors` (the streaming module raises a subclass of it and cannot import this package); it is exported here too, where it
always was."""

from typing import Any

from pydantic import ValidationError

from featherweight.errors import BadRequest as BadRequest

__all__ = ["BadRequest", "problems"]


# The tags of the unions in an AG-UI request: the role of a message, the type of a part, the type of a source.
_ROLES = {"user", "assistant", "system", "developer", "tool", "activity", "reasoning"}
_PARTS = {"text", "image", "audio", "video", "document", "binary"}
_SOURCES = {"data", "url"}


def _path(loc: tuple[Any, ...]) -> str:
    """Where in the request, as `messages[0].content[0].source.mimeType`: pydantic's path for a union also names the branches it
    tried (`list[tagged-union[...]]`) and the tag of each (`user`, `image`, `data`), which say nothing to the person who sent it."""
    kept: list[Any] = []
    tagged = False  # a tag was just skipped: the next name is a field, even if it is spelled like a tag (`text`)
    for segment in loc:
        if isinstance(segment, int):
            kept.append(segment)
            tagged = False
        elif "[" in segment:
            continue  # the name of a branch of a union
        elif (
            not tagged
            and kept
            and (
                (segment in _ROLES and isinstance(kept[-1], int) and len(kept) > 1 and kept[-2] == "messages")
                or (segment in _PARTS and isinstance(kept[-1], int) and len(kept) > 1 and kept[-2] == "content")
                or (segment in _SOURCES and kept[-1] == "source")
            )
        ):
            tagged = True
        else:
            kept.append(segment)
            tagged = False
    text = ""
    for segment in kept:
        text += f"[{segment}]" if isinstance(segment, int) else (f".{segment}" if text else segment)
    return text or "body"


def problems(exc: ValidationError) -> str:
    """What is wrong in a request, from a pydantic error, in the words of both adapters: one line per problem, `where: what`. The
    message of a rule of ours (a `ValueError` of a validator) already says where, and stands alone, without pydantic's "Value error, ".
    When a field takes a string or a list, the complaint that a list is not a string is left out when something else is wrong."""
    errors = exc.errors()
    if len(errors) > 1:
        errors = [
            e for e in errors if not (e["type"] == "string_type" and e["loc"] and e["loc"][-1] == "str")
        ] or errors
    lines = []
    for error in errors:
        message = error["msg"]
        if error["type"] == "value_error" and not error["loc"]:
            lines.append(message.removeprefix("Value error, "))
        else:
            lines.append(f"{_path(error['loc'])}: {message.removeprefix('Value error, ')}")
    return "; ".join(lines)
