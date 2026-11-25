"""Streaming agents: an agent that talks to a person while it works.

A `StreamingAgent` is run with `runner.stream()`, which returns a `StreamingRun`: an async iterable of AG-UI events
(the events of the `ag-ui-protocol` package, which `featherweight` does not redefine). There is no output model, no output
check and no retry. See docs/streaming.md.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from typing import Any, ClassVar

from ag_ui.core import BaseEvent, Message, RunAgentInput, TokenUsage
from langchain_core.messages import BaseMessage
from pydantic import Field, model_validator

from featherweight.db import new_id
from featherweight.errors import AttachmentLimitError
from featherweight.messages import count_attachments, to_agui, to_langchain
from featherweight.models import AgentBase, AgentInput, Status

logger = logging.getLogger(__name__)

_END = object()


class ChatInput(AgentInput):
    """The conversation so far, sent by the client. `featherweight` keeps no conversation: this is all the agent sees.

    The last message is from the user. `system` and `developer` messages are refused, so that a client cannot replace
    the agent's prompt. `thread_id` names the conversation in the run log; it does not load anything."""

    messages: list[Message] = Field(min_length=1)
    thread_id: str | None = None

    @model_validator(mode="after")
    def _check_messages(self) -> "ChatInput":
        for index, message in enumerate(self.messages):
            if message.role in ("system", "developer"):
                raise ValueError(
                    f"messages[{index}] is a {message.role} message: a client cannot send one, "
                    "the agent has its own prompt"
                )
        if self.messages[-1].role != "user":
            raise ValueError(f"The last message must be from the user, not {self.messages[-1].role}")
        to_langchain(self.messages)  # refuses what cannot be converted, here and not in the middle of a run
        return self

    @classmethod
    def from_langchain(cls, messages: Sequence[BaseMessage], thread_id: str | None = None) -> "ChatInput":
        """The conversation from LangChain messages, such as the ones a chat message history holds. The last is the user's."""
        return cls(messages=to_agui(messages), thread_id=thread_id)

    @classmethod
    def from_agui(cls, request: RunAgentInput | Sequence[Message], thread_id: str | None = None) -> "ChatInput":
        """The conversation from what an AG-UI client sent: a `RunAgentInput` (its thread id is kept) or its messages."""
        if isinstance(request, RunAgentInput):
            return cls(messages=list(request.messages), thread_id=thread_id or request.thread_id)
        return cls(messages=list(request), thread_id=thread_id)


class StreamingAgent(AgentBase):
    """An agent for chat and other user-facing work. It has no `output_model` and no `check_output`, and is not retried.

    Declare it as any agent (name, description, version, prompt, model, tools, skills, MCP servers, sub-agents). Its
    input is a `ChatInput`. A sub-agent it launches is a tool call that returns when the sub-agent is done; its own text
    is not streamed. `allow_user_questions` and `raise_conflicts` do nothing here: a person is already in the chat."""

    input_model: ClassVar[type[AgentInput]] = ChatInput
    streaming: ClassVar[bool] = True
    # What the attachments of a conversation (all its messages, each turn) may come to; the application raises or lowers them
    # on its agent. `max_bytes` is the decoded size. See docs/specs/attachments.md.
    max_attachments: ClassVar[int] = 10
    max_bytes: ClassVar[int] = 15 * 1024 * 1024

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for attribute in ("max_attachments", "max_bytes"):
            value = getattr(cls, attribute)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{cls.__name__}.{attribute} must be an integer of 0 or more, got {value!r}")

    @classmethod
    def check_attachments(cls, chat_input: "ChatInput") -> None:
        """Raises `AttachmentLimitError` (a `BadRequest`, so a `ValueError`) when the conversation has more attachments, or more bytes
        of them, than this agent allows."""
        count, size = count_attachments(chat_input.messages)
        if count > cls.max_attachments:
            raise AttachmentLimitError(
                f"The conversation has {count} attachments, {count - cls.max_attachments} over the {cls.max_attachments} "
                f"{cls.__name__} allows (all the messages count, not only the last). The application can raise "
                f"max_attachments on the agent."
            )
        if size > cls.max_bytes:
            raise AttachmentLimitError(
                f"The attachments of the conversation are {size} bytes, {size - cls.max_bytes} over the {cls.max_bytes} "
                f"{cls.__name__} allows (decoded size, all the messages count). The application can raise max_bytes "
                f"on the agent."
            )

    def _leading_prompt_parts(self) -> list[str]:
        return [self.prompt]


class StreamingRun:
    """One streamed turn. Loop over it to get the events; it can be looped over once.

    The run starts when the loop starts, and ends when the loop ends. Leaving the loop early stops the run.
    `status` is `INIT` until the loop starts, then `RUNNING`, then `SUCCESS`, `FAILED` or `CANCELLED`; `error` says why it failed.
    `usage` is the tokens of the turn, known when the run ends. `new_messages` is what the turn produced."""

    def __init__(self, run_id: str, start: Callable[["StreamingRun", Callable[[BaseEvent], None]], Awaitable[None]]):
        self.run_id = run_id  # the run: what the events of the stream carry (AG-UI's `run_id`)
        self.attempt_id = new_id()  # its one attempt: the row of the run log
        self.status = Status.INIT
        self.error: str | None = None
        self.usage: TokenUsage | None = None
        self._new_messages: list[BaseMessage] = []
        self._start = start
        self._used = False
        self._cancel_requested = False
        self._interrupt: Callable[[], object] | None = None  # set by the runner while the model is streaming

    @property
    def new_messages(self) -> list[BaseMessage]:
        """The LangChain messages this turn produced: the assistant messages (with their tool calls and thoughts) and the
        tool results, in order. They fill up as the turn goes and are complete when the loop ends. Store them next to the
        history you sent: `history.add_messages(run.new_messages)`."""
        return list(self._new_messages)

    def cancel(self) -> None:
        """Stop the run, from anywhere in the same process. The model call is cancelled, the stream ends cleanly (the open
        things are closed and `RUN_FINISHED` has the `cancelled` outcome), the row is `CANCELLED`, and `new_messages` has what
        the turn had said and done. A tool that was running is cancelled where it can be (an async tool); a synchronous tool
        or an MCP call may finish anyway. Safe to call before the loop starts, after the run ended, or twice."""
        self._cancel_requested = True
        if self._interrupt is not None:
            self._interrupt()

    def __aiter__(self) -> AsyncGenerator[BaseEvent, None]:
        if self._used:
            raise RuntimeError("A streaming run can be looped over once")
        self._used = True
        return self._events()

    async def _events(self) -> AsyncGenerator[BaseEvent, None]:
        """The run works in a task of its own, so that its cleanup runs whatever the reader does."""
        queue: asyncio.Queue[object] = asyncio.Queue()

        async def work() -> None:
            try:
                await self._start(self, queue.put_nowait)
            finally:
                queue.put_nowait(_END)

        task = asyncio.create_task(work())
        try:
            while (item := await queue.get()) is not _END:
                yield item  # ty: ignore[invalid-yield]
            await task  # raises what the run raised, which is a mistake in the caller's code
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
