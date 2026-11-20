"""Observers: what the runner tells the outside world, as it happens. featherweight stores nothing from them.

An observer overrides the hooks it cares about; every hook does nothing by default. The runner calls them inline, with a
timeout, and ignores their errors: an observer never changes or fails a run. The records carry metadata always, and content
(inputs, outputs, messages, error text) only for an observer whose `capture_content` is true.
"""

import asyncio
import contextlib
import logging
import time
from abc import ABC
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from functools import partial
from typing import Any, ClassVar
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler
from pydantic import BaseModel, ConfigDict

from featherweight.context import CURRENT_TOOL_CALL_ID
from featherweight.models import RunStatus

logger = logging.getLogger("featherweight")


class _Record(BaseModel):
    """A frozen record. `content_fields` are the ones that hold content: they stay `None` for an observer that did not ask."""

    model_config = ConfigDict(frozen=True)
    content_fields: ClassVar[tuple[str, ...]] = ()


class JobStart(_Record):
    """A job begins to run (in your process or on a worker)."""

    content_fields: ClassVar[tuple[str, ...]] = ("input",)

    job_id: str
    name: str
    version: str | None = None
    job_hash: str | None = None
    input_hash: str | None = None
    resumable: bool = False
    resumed_from: str | None = None
    execution: int | None = (
        None  # 1 for the first execution of the job, one more for each takeover; None if there is no owner
    )
    trace_context: Mapping[str, str] = {}  # the carrier that came with the message, if any
    input: dict[str, Any] | None = None


class JobEnd(_Record):
    content_fields: ClassVar[tuple[str, ...]] = ("output", "error")

    status: RunStatus
    error_type: str | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class JobSubmitted(_Record):
    """A client submitted a job. An observer may return the carrier that travels in the message."""

    content_fields: ClassVar[tuple[str, ...]] = ("input",)

    job_id: str
    name: str
    version: str | None = None
    job_hash: str | None = None
    input_hash: str | None = None
    input: dict[str, Any] | None = None


class AttemptStart(_Record):
    """One attempt of an agent begins: one per row of `agent_runs`."""

    content_fields: ClassVar[tuple[str, ...]] = ("input", "system_prompt")

    agent_run_id: str
    invocation_id: str
    attempt: int
    max_attempts: int
    job_id: str | None = None
    parent_agent_run_id: str | None = None
    parent_tool_call_id: str | None = None  # the tool call (of the parent attempt) that launched this agent
    depth: int = 1
    agent_name: str
    agent_version: str
    agent_description: str = ""
    agent_hash: str
    provider: str
    model: str
    streaming: bool = False
    input: dict[str, Any] | None = None
    system_prompt: str | None = None


class AttemptEnd(_Record):
    content_fields: ClassVar[tuple[str, ...]] = ("output", "error")

    status: RunStatus
    final: bool  # the attempt succeeded, or it was the last one allowed
    input_tokens: int = 0
    output_tokens: int = 0
    reported_model: str | None = None
    latency_ms: float = 0.0
    error_type: str | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class ReusedRun(_Record):
    """A step of a resumed job was answered from the result store: it is not an attempt, and has no model calls."""

    content_fields: ClassVar[tuple[str, ...]] = ("output",)

    agent_run_id: str
    invocation_id: str
    reused_from_agent_run_id: str
    resumed_from: str | None = None
    job_id: str | None = None
    agent_name: str
    agent_version: str
    agent_hash: str
    output: dict[str, Any] | None = None


class ModelCallStart(_Record):
    content_fields: ClassVar[tuple[str, ...]] = ("messages",)

    call_id: str
    agent_run_id: str  # the attempt that made it
    provider: str
    model: str
    messages: list[dict[str, Any]] | None = None


class ModelCallEnd(_Record):
    content_fields: ClassVar[tuple[str, ...]] = ("output", "error")

    call_id: str
    agent_run_id: str
    response_model: str | None = None
    finish_reasons: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: float = 0.0
    error_type: str | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class ToolCallStart(_Record):
    content_fields: ClassVar[tuple[str, ...]] = ("arguments",)

    call_id: str  # the id the model gave the call: a launched agent's `parent_tool_call_id` is this
    agent_run_id: str
    tool_name: str
    arguments: dict[str, Any] | None = None


class ToolCallEnd(_Record):
    content_fields: ClassVar[tuple[str, ...]] = ("result", "error")

    call_id: str
    agent_run_id: str
    tool_name: str
    duration_ms: float = 0.0
    error_type: str | None = None
    result: Any = None
    error: str | None = None


def strip_content[R: _Record](record: R) -> R:
    """The record without its content fields."""
    if not record.content_fields:
        return record
    return record.model_copy(update=dict.fromkeys(record.content_fields))


class Scope:
    """What `job()` and `attempt()` yield: the observer's handle on the work that runs inside it. `finish(end)` is called with the
    outcome (a `JobEnd` or an `AttemptEnd`) before the scope is left."""

    async def finish(self, end: JobEnd | AttemptEnd) -> None:
        return None


@contextlib.asynccontextmanager
async def _no_scope() -> AsyncGenerator[Scope]:
    yield Scope()


class Observer(ABC):  # noqa: B024  # the hooks are optional: every one has a default that does nothing
    """Hears what the runner does. Override the hooks you want; each does nothing by default.

    `job` and `attempt` wrap the work (so an observer can make its span the current one while it runs); the model-call and
    tool-call hooks are events, because they come from a callback in another task. Check an implementation with
    `featherweight.testing.check_observer`."""

    capture_content: ClassVar[bool] = (
        False  # content (inputs, outputs, messages) is built, and passed, only if this is true
    )

    def job(self, start: JobStart) -> AbstractAsyncContextManager[Scope]:
        return _no_scope()

    def attempt(self, start: AttemptStart) -> AbstractAsyncContextManager[Scope]:
        return _no_scope()

    async def reused(self, run: ReusedRun) -> None:
        return None

    async def model_call_started(self, call: ModelCallStart) -> None:
        return None

    async def model_call_finished(self, call: ModelCallEnd) -> None:
        return None

    async def tool_call_started(self, call: ToolCallStart) -> None:
        return None

    async def tool_call_finished(self, call: ToolCallEnd) -> None:
        return None

    async def submitted(self, job: JobSubmitted) -> Mapping[str, str] | None:
        """A job was submitted. Return the carrier (for example the W3C trace context) that should travel in the message."""
        return None


class Entered:
    """The scopes of every observer, entered for one job or one attempt. `leave(end)` finishes and leaves them, once."""

    def __init__(
        self,
        observers: "Observers",
        entered: list[tuple[Observer, AbstractAsyncContextManager[Scope], Scope | None]],
    ):
        self._observers = observers
        self._entered = entered
        self._left = False

    async def leave(self, end: JobEnd | AttemptEnd | None = None) -> None:
        if self._left:
            return
        self._left = True
        for observer, manager, scope in reversed(self._entered):
            if scope is not None and end is not None:
                await self._observers.safely(
                    partial(scope.finish, self._observers.view(observer, end)), "finish", observer
                )
            await self._observers.safely(partial(manager.__aexit__, None, None, None), "exit", observer)


class Observers:
    """The observers of a runner: calls each inline, with a timeout, and ignores what they raise."""

    def __init__(self, observers: Sequence[Observer] = (), timeout: float = 1.0):
        self.observers = list(observers)
        self.timeout = timeout
        self.wants_content = any(o.capture_content for o in self.observers)

    def __bool__(self) -> bool:
        return bool(self.observers)

    def view[R: _Record](self, observer: Observer, record: R) -> R:
        """The record as this observer may see it: content is dropped for one that did not ask."""
        return record if observer.capture_content else strip_content(record)

    async def safely[T](
        self, call: Callable[[], Awaitable[T]], hook: str, observer: Observer | None = None
    ) -> T | None:
        """Make the call and await it with the timeout; what it raises, in either step, is logged and ignored (a cancellation is not)."""
        try:
            async with asyncio.timeout(self.timeout):
                return await call()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("observer_timeout observer=%s hook=%s timeout_s=%s", _name(observer), hook, self.timeout)
        except Exception as exc:
            logger.warning(
                "observer_failed observer=%s hook=%s error=%s: %s", _name(observer), hook, type(exc).__name__, exc
            )
        return None

    async def enter(self, hook: str, start: _Record) -> Entered:
        """Enter `observer.job(start)` or `observer.attempt(start)` of every observer."""
        entered: list[tuple[Observer, AbstractAsyncContextManager[Scope], Scope | None]] = []
        for observer in self.observers:
            manager = await self.safely(
                partial(self._manager, observer, hook, start), f"{hook}.enter", observer
            )  # a hook that raises when it is called is guarded too
            if manager is not None:
                scope = await self.safely(manager.__aenter__, f"{hook}.enter", observer)
                entered.append((observer, manager, scope))
        return Entered(self, entered)

    async def _manager(self, observer: Observer, hook: str, start: _Record) -> AbstractAsyncContextManager[Scope]:
        return getattr(observer, hook)(self.view(observer, start))

    async def emit(self, hook: str, record: _Record) -> None:
        """Call an event hook of every observer."""
        for observer in self.observers:
            await self.safely(partial(getattr(observer, hook), self.view(observer, record)), hook, observer)

    async def submitted(self, record: JobSubmitted) -> dict[str, str]:
        """The carriers that the observers return for a submitted job, merged."""
        carrier: dict[str, str] = {}
        for observer in self.observers:
            got = await self.safely(partial(observer.submitted, self.view(observer, record)), "submitted", observer)
            if got:
                carrier.update(got)
        return carrier

    def callbacks(self, agent_run_id: str, provider: str, model: str) -> list[BaseCallbackHandler]:
        """The LangChain callback handler for one attempt, or none when there is no observer."""
        return [ObserverCallbacks(self, agent_run_id, provider, model)] if self else []


def _name(observer: Observer | None) -> str:
    return type(observer).__name__ if observer is not None else "-"


class ObserverCallbacks(AsyncCallbackHandler):
    """Turns LangChain's model and tool callbacks, inside one attempt, into the observers' events, with real timings."""

    run_inline = True  # in order, in the task that made the call
    raise_error = False

    def __init__(self, observers: Observers, agent_run_id: str, provider: str, model: str):
        self._observers = observers
        self._agent_run_id = agent_run_id
        self._provider = provider
        self._model = model
        self._started: dict[UUID, tuple[float, str, str]] = {}  # run id -> start time, call id, tool name
        self._tokens: dict[UUID, Any] = {}  # run id -> the token that set CURRENT_TOOL_CALL_ID

    async def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], *, run_id: UUID, **kwargs: Any
    ) -> None:
        call_id = str(run_id)
        self._started[run_id] = (time.perf_counter(), call_id, "")
        content = [_message(m) for m in (messages[0] if messages else [])] if self._observers.wants_content else None
        await self._observers.emit(
            "model_call_started",
            ModelCallStart(
                call_id=call_id,
                agent_run_id=self._agent_run_id,
                provider=self._provider,
                model=self._model,
                messages=content,
            ),
        )

    async def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        started, call_id, _ = self._started.pop(run_id, (time.perf_counter(), str(run_id), ""))
        message = response.generations[0][0].message if response.generations and response.generations[0] else None
        usage = getattr(message, "usage_metadata", None) or {}
        meta = getattr(message, "response_metadata", None) or {}
        reason = meta.get("finish_reason") or meta.get("stop_reason")
        await self._observers.emit(
            "model_call_finished",
            ModelCallEnd(
                call_id=call_id,
                agent_run_id=self._agent_run_id,
                response_model=meta.get("model_name") or meta.get("model"),
                finish_reasons=[str(reason)] if reason else [],
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                duration_ms=(time.perf_counter() - started) * 1000,
                output=_message(message) if message is not None and self._observers.wants_content else None,
            ),
        )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        started, call_id, _ = self._started.pop(run_id, (time.perf_counter(), str(run_id), ""))
        await self._observers.emit(
            "model_call_finished",
            ModelCallEnd(
                call_id=call_id,
                agent_run_id=self._agent_run_id,
                duration_ms=(time.perf_counter() - started) * 1000,
                error_type=type(error).__name__,
                error=str(error) if self._observers.wants_content else None,
            ),
        )

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        call_id = str(kwargs.get("tool_call_id") or run_id)
        name = str(kwargs.get("name") or serialized.get("name") or "")
        self._started[run_id] = (time.perf_counter(), call_id, name)
        self._tokens[run_id] = CURRENT_TOOL_CALL_ID.set(call_id)  # the tool, and an agent it launches, read it
        await self._observers.emit(
            "tool_call_started",
            ToolCallStart(
                call_id=call_id,
                agent_run_id=self._agent_run_id,
                tool_name=name,
                arguments=(inputs if inputs is not None else {"input": input_str})
                if self._observers.wants_content
                else None,
            ),
        )

    def _reset(self, run_id: UUID) -> None:
        token = self._tokens.pop(run_id, None)
        if token is not None:
            with contextlib.suppress(ValueError):  # a token can only be reset in the context that made it
                CURRENT_TOOL_CALL_ID.reset(token)

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        started, call_id, name = self._started.pop(run_id, (time.perf_counter(), str(run_id), ""))
        self._reset(run_id)
        await self._observers.emit(
            "tool_call_finished",
            ToolCallEnd(
                call_id=call_id,
                agent_run_id=self._agent_run_id,
                tool_name=name,
                duration_ms=(time.perf_counter() - started) * 1000,
                result=getattr(output, "content", output) if self._observers.wants_content else None,
            ),
        )

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        started, call_id, name = self._started.pop(run_id, (time.perf_counter(), str(run_id), ""))
        self._reset(run_id)
        await self._observers.emit(
            "tool_call_finished",
            ToolCallEnd(
                call_id=call_id,
                agent_run_id=self._agent_run_id,
                tool_name=name,
                duration_ms=(time.perf_counter() - started) * 1000,
                error_type=type(error).__name__,
                error=str(error) if self._observers.wants_content else None,
            ),
        )


def _message(message: Any) -> dict[str, Any]:
    """A LangChain message as a plain dict: its role and content, and the tool calls it makes."""
    out: dict[str, Any] = {"role": getattr(message, "type", "unknown"), "content": getattr(message, "content", "")}
    if getattr(message, "tool_calls", None):
        out["tool_calls"] = [dict(call) for call in message.tool_calls]
    return out
