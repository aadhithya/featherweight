"""What `ChatInput` refuses in the data of an attachment, and how: the one form of base64 that is taken, the error that names the
likely cause, the empty attachment, that the check agrees with Python's, and that it makes no decoded copy. No model, no network."""

import base64
import binascii
import random
import tracemalloc

import pytest
from ag_ui.core import (
    DocumentInputContent,
    ImageInputContent,
    InputContentDataSource,
    TextInputContent,
    UserMessage,
)
from pydantic import ValidationError

from featherweight import messages
from featherweight.messages import to_langchain
from featherweight.streaming import ChatInput

PNG = base64.b64encode(b"\x89PNG fake image bytes").decode()
PADDED = base64.b64encode(b"two!!").decode()  # "dHdvISE=": it ends in padding


def chat(value: str, kind=ImageInputContent, mime: str = "image/png") -> ChatInput:
    part = kind(source=InputContentDataSource(value=value, mime_type=mime))
    return ChatInput(messages=[UserMessage(id="u1", content=[TextInputContent(text="look"), part])])


# the one form that is taken, and what is said about a mistake


def test_what_b64encode_gives_is_taken():
    for size in range(0, 12):
        if size:  # an empty one is refused, see below
            chat(base64.b64encode(b"x" * size).decode())


@pytest.mark.parametrize(
    ("value", "says"),
    [
        (f"data:image/png;base64,{PNG}", "it is a data: URL. Send only the part after the comma"),
        (base64.encodebytes(b"x" * 100).decode(), "it has line breaks or spaces"),
        (PNG[:8] + " " + PNG[8:], "it has line breaks or spaces"),
        (PNG + "\n", "it has line breaks or spaces"),
        (base64.urlsafe_b64encode(b"\xfb\xff\xfe" * 4).decode(), "URL-safe alphabet"),
        (PADDED.rstrip("="), "it is not padded"),
        ("@@@@", "characters outside the base64 alphabet"),
        ("AA==AAAA", "padding may only end the data"),
    ],
    ids=[
        "data-url",
        "wrapped",
        "space",
        "trailing-newline",
        "url-safe",
        "unpadded",
        "characters",
        "padding-in-the-middle",
    ],
)
def test_data_that_is_not_base64_is_refused_and_the_error_names_the_likely_cause(value, says):
    assert value != PNG and value != PADDED
    with pytest.raises(ValidationError, match="whose data is not base64") as refused:
        chat(value)
    assert says in str(refused.value)
    assert "messages[0].content[1] is image content" in str(refused.value)  # it names the part


def test_the_error_never_quotes_the_data():
    secret = "SECRETSECRETSECRETSECRET\n"
    with pytest.raises(ValidationError) as refused:
        chat(secret)
    assert "SECRETSECRET" not in str(refused.value)


@pytest.mark.parametrize("kind, mime", [(ImageInputContent, "image/png"), (DocumentInputContent, "application/pdf")])
def test_an_empty_attachment_is_refused_when_the_input_is_built(kind, mime):
    with pytest.raises(ValidationError, match="with no data: an empty attachment is refused"):
        chat("", kind, mime)


def test_an_empty_attachment_in_a_stored_conversation_is_refused_too():
    stored = to_langchain(
        [UserMessage(id="u1", content=[TextInputContent(text="hi"), ImageInputContent(source=_source(PNG))])]
    )
    stored[0].content[1]["base64"] = ""
    with pytest.raises(ValueError, match="not inline data"):  # `to_agui` takes no block that has no bytes
        ChatInput.from_langchain([*stored])


def _source(value: str) -> InputContentDataSource:
    return InputContentDataSource(value=value, mime_type="image/png")


# the check agrees with Python's, in chunks


def accepts(value: str) -> bool:
    try:
        messages._check_base64(value)
    except (binascii.Error, ValueError):
        return False
    return True


def python_accepts(value: str) -> bool:
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


@pytest.mark.parametrize("chunk", [4, 8, 12])
def test_the_check_agrees_with_python_over_many_strings_and_at_the_edges_of_the_chunks(monkeypatch, chunk):
    monkeypatch.setattr(messages, "_CHUNK", chunk)
    rng = random.Random(7)
    pieces = "ABCDabcd0189+/=-_ \n"
    for _ in range(4000):
        value = "".join(rng.choice(pieces) for _ in range(rng.randrange(0, 40)))
        assert accepts(value) == python_accepts(value), repr(value)
    for size in range(0, 70):  # real base64 of every length is taken
        value = base64.b64encode(rng.randbytes(size)).decode()
        assert accepts(value), size


def test_padding_in_the_middle_is_refused_even_when_it_falls_at_the_end_of_a_chunk(monkeypatch):
    monkeypatch.setattr(messages, "_CHUNK", 4)
    assert not accepts("AA==AAAA")
    assert accepts("AAAAAA==")


# no decoded copy


def peak(function, *args) -> int:
    tracemalloc.start()
    try:
        function(*args)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_checking_a_large_attachment_allocates_no_decoded_copy_of_it():
    value = base64.b64encode(b"x" * (24 * 1024 * 1024)).decode()  # 32 MB of text; built before the measure starts
    whole = peak(lambda v: base64.b64decode(v, validate=True), value)
    assert whole > 20 * 1024 * 1024  # the measure can see a full decode (the control)
    assert peak(messages._check_base64, value) < 1024 * 1024
    assert (
        peak(chat, value) < 1024 * 1024
    )  # and through a `ChatInput`, which holds the string it was given and nothing else
