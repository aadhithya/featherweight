"""Streaming agents: an agent that talks to a person while it works.

A `StreamingAgent` is run with `runner.stream()`, which returns a `StreamingRun`: an async iterable of AG-UI events
(the events of the `ag-ui-protocol` package, which `micro` does not redefine). There is no output model, no output
check and no retry. See docs/streaming.md.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import ClassVar

from ag_ui.core import BaseEvent, Message, TokenUsage
from pydantic import Field, model_validator

from micro.messages import check_supported
from micro.models import AgentBase, AgentInput, RunStatus

logger = logging.getLogger(__name__)

_END = object()


class ChatInput(AgentInput):
    """The conversation so far, sent by the client. `micro` keeps no conversation: this is all the agent sees.

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
        check_supported(self.messages)
        return self


class StreamingAgent(AgentBase):
    """An agent for chat and other user-facing work. It has no `output_model` and no `check_output`, and is not retried.

    Declare it as any agent (name, description, version, prompt, model, tools, skills, MCP servers, sub-agents). Its
    input is a `ChatInput`. A sub-agent it launches is a tool call that returns when the sub-agent is done; its own text
    is not streamed. `allow_user_questions` and `raise_conflicts` do nothing here: a person is already in the chat."""

    input_model: ClassVar[type[AgentInput]] = ChatInput
    streaming: ClassVar[bool] = True

    def _leading_prompt_parts(self) -> list[str]:
        return [self.prompt]


class StreamingRun:
    """One streamed turn. Loop over it to get the events; it can be looped over once.

    The run starts when the loop starts, and ends when the loop ends. Leaving the loop early stops the run.
    `status` is `INIT` until the loop starts, then `RUNNING`, then `SUCCESS` or `FAILED`; `error` says why it failed.
    `usage` is the tokens of the turn, known when the run ends."""

    def __init__(self, run_id: str, start: Callable[["StreamingRun", Callable[[BaseEvent], None]], Awaitable[None]]):
        self.run_id = run_id
        self.status = RunStatus.INIT
        self.error: str | None = None
        self.usage: TokenUsage | None = None
        self._start = start
        self._used = False

    def __aiter__(self) -> AsyncIterator[BaseEvent]:
        if self._used:
            raise RuntimeError("A streaming run can be looped over once")
        self._used = True
        return self._events()

    async def _events(self) -> AsyncIterator[BaseEvent]:
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
