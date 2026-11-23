"""The limits on attachments: the counting, and `runner.stream()` refusing a conversation over them. Fake models only."""

import base64

import pytest
from ag_ui.core import (
    AssistantMessage,
    DocumentInputContent,
    ImageInputContent,
    InputContentDataSource,
    TextInputContent,
    UserMessage,
)
from fakes import FakeSettings, Streams, runner_with

from featherweight.messages import count_attachments, decoded_size
from featherweight.model_settings import ModelSettings
from featherweight.streaming import ChatInput, StreamingAgent


def b64(size: int) -> str:
    return base64.b64encode(b"x" * size).decode()


def image(size: int = 3) -> ImageInputContent:
    return ImageInputContent(source=InputContentDataSource(value=b64(size), mime_type="image/png"))


def doc(size: int = 3) -> DocumentInputContent:
    return DocumentInputContent(source=InputContentDataSource(value=b64(size), mime_type="application/pdf"))


def chat(*parts_per_user_turn, last_text: str = "now") -> ChatInput:
    """A history with one user message per group of parts (each followed by a reply), and a last user message."""
    messages = []
    for index, parts in enumerate(parts_per_user_turn):
        messages.append(UserMessage(id=f"u{index}", content=[TextInputContent(text="q"), *parts]))
        messages.append(AssistantMessage(id=f"a{index}", content="ok"))
    messages.append(UserMessage(id="last", content=last_text))
    return ChatInput(messages=messages)


class Chat(StreamingAgent):
    name: str = "chat"
    description: str = "Talks to the user"
    version: str = "1"
    prompt: str = "You are a helpful chat agent."
    model: ModelSettings = FakeSettings()


# the counting


@pytest.mark.parametrize("size", [0, 1, 2, 3, 4, 5, 100, 1001])
def test_decoded_size_is_exact_without_decoding(size):
    assert decoded_size(b64(size)) == size


def test_count_attachments_counts_all_messages_and_all_media_types():
    counted = count_attachments(chat([image(3), doc(10)], [image(5)], []).messages)
    assert counted == (3, 18)


def test_count_attachments_ignores_text_and_plain_messages():
    assert count_attachments(chat([], []).messages) == (0, 0)


# the limits


async def test_over_by_count_raises_before_a_run_exists(db):
    runner = runner_with(db, Streams(turns=[["hi"]]))
    with pytest.raises(ValueError, match=r"11 attachments, 1 over the 10 .*raise max_attachments"):
        runner.stream(Chat, chat([image() for _ in range(11)]))
    assert await db.list_attempts() == []


async def test_over_by_bytes_raises_and_says_by_how_much(db):
    class Small(Chat):
        max_bytes = 10

    runner = runner_with(db, Streams(turns=[["hi"]]))
    with pytest.raises(ValueError, match=r"14 bytes, 4 over the 10 .*raise max_bytes"):
        runner.stream(Small, chat([image(14)]))
    assert await db.list_attempts() == []


async def test_attachments_of_earlier_messages_count(db):
    runner = runner_with(db, Streams(turns=[["hi"]]))
    history = chat(*[[image()] for _ in range(11)])  # one per earlier message, none in the last
    with pytest.raises(ValueError, match="11 attachments"):
        runner.stream(Chat, history)


async def test_earlier_bytes_count_too(db):
    class Small(Chat):
        max_bytes = 10

    runner = runner_with(db, Streams(turns=[["hi"]]))
    with pytest.raises(ValueError, match="12 bytes"):
        runner.stream(Small, chat([image(6)], [image(6)]))


async def test_exactly_at_the_limits_passes(db):
    class Exact(Chat):
        max_attachments = 2
        max_bytes = 10

    runner = runner_with(db, Streams(turns=[["hi"]]))
    run = runner.stream(Exact, chat([image(5)], [doc(5)]))
    assert [event async for event in run]
    assert len(await db.list_attempts()) == 1


async def test_a_subclass_can_lower_the_limits(db):
    class Lower(Chat):
        max_attachments = 1

    runner = runner_with(db, Streams(turns=[["hi"]]))
    with pytest.raises(ValueError, match="2 attachments, 1 over the 1 "):
        runner.stream(Lower, chat([image(), image()]))


async def test_a_subclass_can_raise_the_limits(db):
    class Higher(Chat):
        max_attachments = 12
        max_bytes = 100

    runner = runner_with(db, Streams(turns=[["hi"]]))
    run = runner.stream(Higher, chat([image(8) for _ in range(11)]))
    assert [event async for event in run]


async def test_a_turn_without_attachments_is_unaffected(db):
    runner = runner_with(db, Streams(turns=[["hi"]]))
    run = runner.stream(Chat, chat(last_text="hello"))
    assert [event async for event in run]


def test_the_defaults():
    assert StreamingAgent.max_attachments == 10
    assert StreamingAgent.max_bytes == 15 * 1024 * 1024


@pytest.mark.parametrize("attribute, value", [("max_attachments", -1), ("max_bytes", 1.5), ("max_bytes", "10")])
def test_a_bad_limit_is_refused_when_the_class_is_declared(attribute, value):
    with pytest.raises(TypeError, match=attribute):
        type("Bad", (Chat,), {attribute: value})
