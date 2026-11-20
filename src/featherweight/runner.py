"""AgentRunner: runs an agent definition through langgraph, validates the output, retries, and logs."""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    TextMessageContentEvent,
    TokenUsage,
)
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from pydantic import ValidationError

from featherweight.context import (
    CURRENT_AGENT_RUN_ID,
    CURRENT_JOB_EXECUTION,
    CURRENT_TOOL_CALL_ID,
    IN_BACKGROUND_JOB,
    RunContext,
)
from featherweight.db import FINAL, AgentRunRow, Database, RunEnd, RunStart, new_id
from featherweight.discovery import discovery_tools
from featherweight.errors import RETRYABLE_4XX, SetupError
from featherweight.fingerprint import agent_hash, error_fields, input_hash, output_hash, text_hash, tools_hash
from featherweight.mcp_connections import Lease, MCPConnections
from featherweight.messages import to_langchain
from featherweight.models import (
    BACKGROUND_TOOL_NAMES,
    DISCOVERY_TOOL_NAMES,
    LAUNCH_TOOL_NAMES,
    SKILL_TOOL_NAMES,
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentResult,
    RunStatus,
    entry_description,
    entry_name,
)
from featherweight.observers import AttemptEnd, AttemptStart, Entered, JobSubmitted, Observer, Observers, ReusedRun
from featherweight.registry import ToolRegistry
from featherweight.results import ResultKey, ResultStore, SqliteResultStore
from featherweight.sqlite import SqliteDatabase
from featherweight.store import RunStore
from featherweight.stream_events import EventMapper
from featherweight.streaming import ChatInput, StreamingAgent, StreamingRun

if TYPE_CHECKING:
    from featherweight.client import JobClient

logger = logging.getLogger(__name__)

ENV_MAX_CONCURRENT = "MAX_CONCURRENT_AGENT_RUNS"
DEFAULT_MAX_CONCURRENT = 10
# How far agents may launch agents: the most agents in one chain (A launches B launches C is 3), and the most launches
# one attempt of one agent may make. A depth of 1 turns launching off.
ENV_MAX_DEPTH = "MAX_AGENT_DEPTH"
DEFAULT_MAX_DEPTH = 3
ENV_MAX_LAUNCHES = "MAX_AGENT_LAUNCHES"
DEFAULT_MAX_LAUNCHES = 10
# The most children one attempt of one agent may have running in the background at one time.
DEFAULT_MAX_BACKGROUND = 3

MAX_ERROR_CHARS = 2000


class ModelSetupError(SetupError):
    """The chat model could not be built (e.g. no API key). Retrying cannot help."""


def is_non_retryable(exc: BaseException) -> bool:
    """Auth / config style errors, where another attempt would fail the same way."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SetupError):
            return True
        for attr in ("code", "status_code"):
            code = getattr(current, attr, None)
            if isinstance(code, int) and 400 <= code < 500 and code not in RETRYABLE_4XX:
                return True
        current = current.__cause__
    return False


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def max_concurrent_from_env() -> int:
    """The concurrency limit from `MAX_CONCURRENT_AGENT_RUNS`, or the default when unset."""
    return _positive_int_from_env(ENV_MAX_CONCURRENT, DEFAULT_MAX_CONCURRENT)


def max_depth_from_env() -> int:
    """The launch depth limit from `MAX_AGENT_DEPTH`, or the default when unset."""
    return _positive_int_from_env(ENV_MAX_DEPTH, DEFAULT_MAX_DEPTH)


def max_launches_from_env() -> int:
    """The launches one attempt may make, from `MAX_AGENT_LAUNCHES`, or the default when unset."""
    return _positive_int_from_env(ENV_MAX_LAUNCHES, DEFAULT_MAX_LAUNCHES)


def _format_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


def _usage(messages: list[Any]) -> tuple[int, int, str | None]:
    """Sum token usage over the AI messages of a run; also return the API-reported model name."""
    input_tokens = output_tokens = 0
    reported_model: str | None = None
    for message in messages:
        if isinstance(message, AIMessage):
            usage = message.usage_metadata or {}
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
            reported_model = message.response_metadata.get("model_name") or reported_model
    return input_tokens, output_tokens, reported_model


class _Child:
    """A background child that a run started: its job, and its task when it runs here (`None` on a worker)."""

    __slots__ = ("agent", "job_id", "task")

    def __init__(self, job_id: str, agent: str, task: "asyncio.Task[Any] | None") -> None:
        self.job_id = job_id
        self.agent = agent
        self.task = task


class _Slot:
    """The concurrency slot of one attempt.

    An agent that launches another agent waits for it, and the child needs a slot of its own, so the parent hands its
    slot back while any of its launches run and takes one again when the last of them is done. Without that, parents
    waiting on children could hold every slot and nothing would ever finish. A model can ask for several launches in
    one turn, which run at the same time, so the hand-back is counted.
    """

    def __init__(self, semaphore: asyncio.Semaphore):
        self._semaphore = semaphore
        self.held = False
        self._away = 0  # launches of this attempt that are running now

    async def acquire(self) -> None:
        await self._semaphore.acquire()
        self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self._semaphore.release()

    def leave(self) -> None:
        self._away += 1
        self.release()

    async def back(self) -> None:
        self._away -= 1
        if self._away == 0:
            await self.acquire()  # if this is cancelled, `held` stays False and the final release does nothing


class _Turn:
    """What the end of a streamed turn needs to know: the mapper and the text written so far."""

    def __init__(self, mapper: EventMapper):
        self.mapper = mapper
        self.output_digest = hashlib.sha256()


def _close_turn(partial: AIMessage | None, messages: list[BaseMessage], why: str) -> list[BaseMessage]:
    """The messages that end a turn that stopped halfway: the message the model was writing (when there is something to keep
    of it), and a tool message for every tool call that has no result, because a provider rejects a call without one."""
    out: list[BaseMessage] = [partial] if partial is not None else []
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    for message in [*messages, *out]:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["id"] and call["id"] not in answered:
                    answered.add(call["id"])
                    out.append(
                        ToolMessage(
                            content=f"This tool did not return: {why}.",
                            tool_call_id=call["id"],
                            name=call["name"],
                            status="error",
                        )
                    )
    return out


def _job_execution(job_id: str | None) -> int | None:
    """The execution of the job that this task runs, if it runs `job_id`: set by `execute_job`. None otherwise (the database then uses
    the job's `executions`, for a run that was not started by a job class)."""
    current = CURRENT_JOB_EXECUTION.get()
    return current[1] if current is not None and current[0] == job_id else None


def _as_job(entry: Any) -> Any:
    from featherweight.jobs import as_job  # jobs.py imports this module

    return as_job(entry)


class AgentRunner:
    def __init__(
        self,
        db: Database,
        *,
        max_attempts: int = 3,
        retry_delay_s: float = 1.0,
        store: RunStore | None = None,
        max_concurrent: int | None = None,
        max_depth: int | None = None,
        max_launches: int | None = None,
        max_background: int | None = None,
        background: "JobClient | None" = None,
        results: ResultStore | None | Literal["default"] = "default",
        observers: Sequence[Observer] = (),
        observer_timeout: float = 1.0,
    ):
        """`store` is what the `store_get` / `store_set` tools read and write (default: a new RunStore).

        `max_concurrent` caps how many attempts (model calls) run at once across this runner. Default:
        `MAX_CONCURRENT_AGENT_RUNS` from the environment, else 10.

        `max_depth` is the most agents in one chain of launches, the first agent included (A launches B launches C is
        3). Default: `MAX_AGENT_DEPTH`, else 3. At 1 no agent can launch another. `max_launches` is the most launches
        one attempt of one agent may make. Default: `MAX_AGENT_LAUNCHES`, else 10.

        `max_background` is the most children one attempt of one agent may have running in the background at one time (default 3).
        A child runs in this process, as a job of its own, unless `background` is a `JobClient`: then it is submitted to the
        queue and a worker runs it (the worker must list the child's job). Agents with `background_agents` need a result store.

        `results` is where the outputs that resuming a job reads back are kept (a `ResultStore`), for the jobs created
        `resumable=True`. With a `SqliteDatabase` the default is a sqlite store in the same file; with any other database there is
        no default, so pass your own. `None` keeps nothing, and then a job that resumes another is a `SetupError`.

        `observers` hear what the runner does (`featherweight.observers`): they are called inline, each with a timeout of
        `observer_timeout` seconds, and what they raise is logged and ignored, so they never change or fail a run.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_concurrent is None:
            max_concurrent = max_concurrent_from_env()
        elif max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        for name, value in (
            ("max_depth", max_depth),
            ("max_launches", max_launches),
            ("max_background", max_background),
        ):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1")
        self.max_depth = max_depth if max_depth is not None else max_depth_from_env()
        self.max_launches = max_launches if max_launches is not None else max_launches_from_env()
        self.max_background = max_background if max_background is not None else DEFAULT_MAX_BACKGROUND
        self.background = background
        self._background_tasks: set[asyncio.Task[Any]] = (
            set()
        )  # the children running here: a reference keeps each from being collected
        self._children: dict[str, list[_Child]] = {}  # parent attempt (or streamed run) id -> the children it started
        self._emitters: dict[
            str, Callable[[BaseEvent], None]
        ] = {}  # a streamed run that is open -> where its events go
        self._watchers: dict[str, list[asyncio.Task[None]]] = {}  # a streamed run -> its tasks that wait for children
        self.max_concurrent = max_concurrent
        self._slots = asyncio.Semaphore(max_concurrent)
        self.store = store or RunStore()
        self.db = db
        self.results: ResultStore | None = (
            results
            if not isinstance(results, str)
            else (SqliteResultStore(db) if isinstance(db, SqliteDatabase) else None)
        )
        self.max_attempts = max_attempts
        self.retry_delay_s = retry_delay_s
        self._observers = Observers(observers, observer_timeout)
        self._keep_open = False  # True inside `async with`: MCP servers stay up between runs
        self._mcp: MCPConnections | None = None  # created when a run first needs an MCP server
        self._streams: dict[str, StreamingRun] = {}  # the streaming runs going on now, by run id, for `cancel()`

    async def __aenter__(self) -> "AgentRunner":
        """Optional. Without it, an MCP server is started when a run needs it and stopped when the last run using it
        ends. Inside `async with`, servers stay up between runs and are stopped when the block ends."""
        self._keep_open = True
        if self._mcp is not None:
            self._mcp.keep_open = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop every MCP server this runner started. Safe to call twice."""
        self._keep_open = False
        mcp, self._mcp = self._mcp, None
        if mcp is not None:
            await mcp.aclose()

    def _connections(self) -> MCPConnections:
        if self._mcp is None:
            self._mcp = MCPConnections(keep_open=self._keep_open)
        return self._mcp

    async def run(
        self, agent_cls: type[AgentBase], agent_input: AgentInput, *, job_id: str | None = None
    ) -> AgentResult[Any]:
        """Run an agent. Runtime failures come back as a FAILED result; they are not raised.

        Programming errors still raise: an input that is not the agent's `input_model`, or a
        `job_id` that does not exist. A streaming agent is run with `stream()`.
        """
        if agent_cls.streaming:
            raise TypeError(f"{agent_cls.__name__} is a streaming agent: run it with runner.stream()")
        return await self._run(agent_cls, agent_input, job_id=job_id)

    def stream(
        self, agent_cls: type[StreamingAgent], chat_input: ChatInput, *, job_id: str | None = None
    ) -> StreamingRun:
        """Run a streaming agent. Returns at once; the run starts when you loop over it, and its events (AG-UI's) come as
        the model writes. A failure is an `RUN_ERROR` event and a FAILED row, not an exception; and nothing is retried.

        Programming errors raise, as for `run()`: an agent that is not a streaming agent, an input that is not a
        `ChatInput`, or a `job_id` that does not exist (raised when the loop starts).
        """
        if not (isinstance(agent_cls, type) and issubclass(agent_cls, StreamingAgent)):
            raise TypeError(
                f"{getattr(agent_cls, '__name__', agent_cls)} is not a streaming agent: run it with runner.run()"
            )
        if not isinstance(chat_input, ChatInput):
            raise TypeError(f"{agent_cls.__name__} expects a ChatInput, got {type(chat_input).__name__}")
        self._check_background_setup(agent_cls())  # ty: ignore[missing-argument]  # a subclass supplies the defaults
        return StreamingRun(new_id(), lambda run, emit: self._stream(agent_cls, chat_input, run, emit, job_id=job_id))

    def cancel(self, run_id: str) -> bool:
        """Stop a streaming run of this runner by its id (the `run_id` of its `RUN_STARTED` event). It ends as `run.cancel()`
        does. Returns False when no such run is going on here."""
        run = self._streams.get(run_id)
        if run is None:
            return False
        run.cancel()
        return True

    async def _stream(
        self,
        agent_cls: type[StreamingAgent],
        chat_input: ChatInput,
        run: StreamingRun,
        emit: Callable[[BaseEvent], None],
        *,
        job_id: str | None,
    ) -> None:
        """The whole of a streamed turn, in the task of the run: the row, the MCP servers, the events, and the end.

        The model is streamed by a task of its own, so that `run.cancel()` can stop it and this task can then end the run
        cleanly, with the events and the row. When the reader leaves instead, this task itself is cancelled: the run ends the
        same way, with no one to send the events to."""
        agent = agent_cls()  # ty: ignore[missing-argument]  # a subclass supplies the defaults
        started = time.perf_counter()
        thread_id = chat_input.thread_id or new_id()  # the protocol needs one; the log keeps only a named one
        can_launch = bool(agent.subagents) and 1 < self.max_depth
        system_prompt = agent.build_system_prompt(can_launch=can_launch, can_background=self._can_background(agent))
        CURRENT_AGENT_RUN_ID.set(run.run_id)  # this task has its own context, so nothing needs undoing
        logger.info(
            "stream_started agent_run_id=%s agent=%s version=%s model=%s reasoning=%s job_id=%s thread_id=%s",
            run.run_id,
            agent.name,
            agent.version,
            agent.model.model,
            agent.model.label(),
            job_id,
            chat_input.thread_id,
        )
        lease = Lease(self._connections)
        scope: Entered | None = None  # the observers' scope of this turn, once its row exists
        try:
            registry = ToolRegistry()
            setup_error: Exception | None = None
            try:
                for local_tool in agent.tools:
                    registry.add(local_tool, "local")
                for server in agent.mcp_servers:
                    await registry.add_mcp(server, lease)
            except Exception as exc:
                setup_error = exc
            await self.db.create_run(
                RunStart(
                    agent_run_id=run.run_id,
                    invocation_id=new_id(),
                    attempt=1,
                    job_id=job_id,
                    job_execution=_job_execution(job_id),
                    agent_name=agent.name,
                    agent_version=agent.version,
                    agent_hash=agent_hash(agent),
                    system_prompt_hash=text_hash(system_prompt),
                    tools_hash=tools_hash(registry.tools, self._runner_tool_names(agent, can_launch)),
                    provider=agent.model.provider,
                    model_name=agent.model.model,
                    reasoning=agent.model.label(),
                    settings=agent.model.model_dump(mode="json"),
                    input_hash=input_hash(chat_input),  # the words stay with the application
                    thread_id=chat_input.thread_id,
                )
            )
            self._streams[run.run_id] = run
            self._emitters[run.run_id] = emit  # `start_background` announces its children on this stream
            if self._observers:
                scope = await self._observers.enter(
                    "attempt",
                    AttemptStart(
                        agent_run_id=run.run_id,
                        invocation_id=run.run_id,
                        attempt=1,
                        max_attempts=1,
                        job_id=job_id,
                        agent_name=agent.name,
                        agent_version=agent.version,
                        agent_description=agent.description,
                        agent_hash=agent_hash(agent),
                        provider=agent.model.provider,
                        model=agent.model.model,
                        streaming=True,
                        input={"messages": [m.model_dump(mode="json") for m in chat_input.messages]}
                        if self._observers.wants_content
                        else None,
                        system_prompt=system_prompt if self._observers.wants_content else None,
                    ),
                )
            turn = _Turn(EventMapper(show_thoughts=agent.model.show_thoughts))
            emit(RunStartedEvent(thread_id=thread_id, run_id=run.run_id))
            consumer = asyncio.create_task(
                self._consume(
                    agent, chat_input, system_prompt, can_launch, registry, setup_error, job_id, run, turn, emit
                )
            )
            run._interrupt = consumer.cancel
            if run._cancel_requested:
                consumer.cancel()
            outcome, failure = "success", None
            try:
                await consumer
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():  # this task is cancelled: the reader left
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
                    outcome = "left"
                else:  # run.cancel() cancelled the consumer
                    outcome = "cancelled"
            except Exception as exc:
                outcome, failure = "failed", exc
            finally:
                run._interrupt = None
            await self._close_background_events(run.run_id)  # nothing is announced after the stream's last event
            if outcome in ("cancelled", "left"):  # a response that was stopped takes its children with it
                await self._cancel_children(run.run_id)
            else:  # a turn that ended on its own leaves them running: the application follows them by their job events
                self._children.pop(run.run_id, None)
            await self._end_stream(run, turn, emit, outcome, failure, agent, thread_id, started, scope)
            if outcome == "left":
                raise asyncio.CancelledError
        finally:
            if scope is not None:
                await scope.leave()  # a no-op once the turn ended; this is for a turn that never got there
            self._streams.pop(run.run_id, None)
            await lease.release()

    async def _consume(
        self,
        agent: StreamingAgent,
        chat_input: ChatInput,
        system_prompt: str,
        can_launch: bool,
        registry: ToolRegistry,
        setup_error: Exception | None,
        job_id: str | None,
        run: StreamingRun,
        turn: "_Turn",
        emit: Callable[[BaseEvent], None],
    ) -> None:
        """Wait for a slot, then stream the model and emit what it writes. Cancelling this task stops the run."""
        slot = _Slot(self._slots)
        try:
            await slot.acquire()  # the row stays INIT while it waits
            await self.db.mark_run_running(run.run_id)
            run.status = RunStatus.RUNNING
            if setup_error is not None:
                raise setup_error
            context = RunContext(store=self.store, job_id=job_id, agent_run_id=run.run_id)
            async for event in self._stream_events(
                agent, chat_input, system_prompt, context, slot, can_launch, registry, turn.mapper, run._new_messages
            ):
                emit(event)
                if isinstance(event, TextMessageContentEvent):
                    turn.output_digest.update(event.delta.encode())
        finally:
            slot.release()

    async def _end_stream(
        self,
        run: StreamingRun,
        turn: "_Turn",
        emit: Callable[[BaseEvent], None],
        outcome: str,
        failure: BaseException | None,
        agent: StreamingAgent,
        thread_id: str,
        started: float,
        scope: Entered | None = None,
    ) -> None:
        """End the run: the events (when someone reads them), the row, and the messages the turn leaves."""
        mapper = turn.mapper
        if outcome != "success":
            # What the model had written when it stopped is a message too, and a tool call that has no result is closed, so
            # that the next request is valid. (The partial message is read before `finish()` clears it.)
            why = "cancelled by the user" if outcome != "failed" else "the run failed"
            run._new_messages.extend(_close_turn(mapper.partial_message(), run._new_messages, why))
        closing = mapper.finish()  # everything that was opened is closed
        usage = TokenUsage(
            provider=agent.model.provider,
            model=agent.model.model,
            input_tokens=mapper.input_tokens,
            output_tokens=mapper.output_tokens,
            total_tokens=mapper.input_tokens + mapper.output_tokens,
        )
        status = {"success": RunStatus.SUCCESS, "failed": RunStatus.FAILED}.get(outcome, RunStatus.CANCELLED)
        error = _format_error(failure) if failure is not None else None  # the client and the logs get the text
        run.status, run.error, run.usage = status, error, usage
        if outcome != "left":
            for event in closing:
                emit(event)
            if outcome == "failed":
                emit(RunErrorEvent(message=error or ""))
            else:
                finished = RunFinishedCancelledOutcome() if outcome == "cancelled" else RunFinishedSuccessOutcome()
                emit(RunFinishedEvent(thread_id=thread_id, run_id=run.run_id, outcome=finished, usage=[usage]))
        error_type, error_digest = error_fields(failure) if failure is not None else (None, None)
        await self.db.finish_run(
            run.run_id,
            RunEnd(
                status=status,
                output_hash=turn.output_digest.hexdigest() if outcome == "success" else None,
                error_type=error_type,
                error_hash=error_digest,
                input_tokens=mapper.input_tokens,
                output_tokens=mapper.output_tokens,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )
        if scope is not None:
            await scope.leave(
                AttemptEnd(
                    status=status,
                    final=True,
                    input_tokens=mapper.input_tokens,
                    output_tokens=mapper.output_tokens,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error_type=error_type,
                    error=error if self._observers.wants_content else None,
                )
            )
        log = logger.info if outcome == "success" else logger.warning
        log("stream_ended agent_run_id=%s outcome=%s error=%s", run.run_id, outcome, error)

    async def _stream_events(
        self,
        agent: StreamingAgent,
        chat_input: ChatInput,
        system_prompt: str,
        context: RunContext,
        slot: _Slot,
        can_launch: bool,
        registry: ToolRegistry,
        mapper: EventMapper,
        new_messages: list[BaseMessage],
    ):
        """Stream the LangChain agent and yield the events of what it writes. The messages it produces are added to
        `new_messages` as each node finishes, so that a turn that is stopped or fails keeps what it had."""
        try:
            model = agent.model.build()
        except Exception as exc:
            raise ModelSetupError(str(exc)) from exc
        self._add_runner_tools(agent, context, slot, 1, can_launch, registry)
        lc_agent = create_agent(model, tools=registry.tools, system_prompt=system_prompt, context_schema=RunContext)
        async for mode, data in lc_agent.astream(
            {"messages": to_langchain(chat_input.messages)},
            config={
                "recursion_limit": agent.recursion_limit,
                "callbacks": self._observers.callbacks(context.agent_run_id, agent.model.provider, agent.model.model),
            },
            context=context,
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                chunk, _metadata = data
                for event in mapper.feed(chunk):
                    yield event
            else:  # "updates": what a finished node returned; the messages are what the application will keep
                for update in data.values() if isinstance(data, dict) else []:
                    for message in (update or {}).get("messages", []):
                        if isinstance(message, AIMessage | ToolMessage):
                            new_messages.append(message)
                        if isinstance(message, AIMessage):
                            mapper.complete_turn()  # the turn is now a whole message: nothing partial is left of it

    @staticmethod
    def _can_background(agent: AgentBase) -> bool:
        """Whether `agent` may start background agents here: it declares some, and this is not itself a background job (what
        a background job runs, inline sub-agents included, cannot start more)."""
        return bool(agent.background_agents) and not IN_BACKGROUND_JOB.get()

    @staticmethod
    def _runner_tool_names(agent: AgentBase, can_launch: bool) -> list[str]:
        """The names of the tools the runner gives `agent`, by what it declares."""
        background = AgentRunner._can_background(agent)
        return (
            (list(LAUNCH_TOOL_NAMES) if can_launch else [])
            + (list(BACKGROUND_TOOL_NAMES) if background else [])
            + (["get_agent_schema"] if background and not can_launch else [])
            + (list(SKILL_TOOL_NAMES) if agent.skills else [])
            + (list(DISCOVERY_TOOL_NAMES) if agent.has_discovery else [])
        )

    def _check_background_setup(self, agent: AgentBase) -> None:
        """An agent that can start background agents needs a result store to read their results from: the client's, when the
        children are submitted to a worker, and the runner's otherwise. A `SetupError` before any model call."""
        if not self._can_background(agent):
            return
        store = self.background.results if self.background is not None else self.results
        if store is None:
            raise SetupError(
                f"{agent.name} can start background agents, which need a result store to read their results from: pass "
                "`results=` to AgentRunner (or a `JobClient` with one as `background=`)"
            )

    def _add_runner_tools(
        self,
        agent: AgentBase,
        context: RunContext,
        slot: _Slot,
        depth: int,
        can_launch: bool,
        registry: ToolRegistry,
    ) -> None:
        for runner_tool in (
            (
                self._launch_tools(agent, context, slot, depth, can_launch)
                if can_launch or self._can_background(agent)
                else []
            )
            + (self._skill_tools(agent) if agent.skills else [])
            + (discovery_tools(registry) if agent.has_discovery else [])
        ):
            registry.add(runner_tool, "runner")

    async def _run(
        self,
        agent_cls: type[AgentBase],
        agent_input: AgentInput,
        *,
        job_id: str | None,
        parent_agent_run_id: str | None = None,
        parent_tool_call_id: str | None = None,
        depth: int = 1,
    ) -> AgentResult[Any]:
        """`run`, for the agents launched by other agents too: `depth` is 1 for a top-level run, and the child shares
        the job and records the attempt that launched it. The run holds a lease on its MCP servers from its first
        attempt to its last, so that a retry does not start them again, and lets go of them when it is done."""
        lease = Lease(self._connections)
        try:
            return await self._invoke(
                agent_cls,
                agent_input,
                lease,
                job_id=job_id,
                parent_agent_run_id=parent_agent_run_id,
                parent_tool_call_id=parent_tool_call_id,
                depth=depth,
            )
        finally:
            await lease.release()

    async def _invoke(
        self,
        agent_cls: type[AgentBase],
        agent_input: AgentInput,
        lease: Lease,
        *,
        job_id: str | None,
        parent_agent_run_id: str | None,
        parent_tool_call_id: str | None,
        depth: int,
    ) -> AgentResult[Any]:
        agent = agent_cls()  # ty: ignore[missing-argument]  # a subclass supplies the defaults
        if not isinstance(agent_input, agent.input_model):
            raise TypeError(f"{agent.name} expects {agent.input_model.__name__}, got {type(agent_input).__name__}")
        self._check_background_setup(agent)

        invocation_id = new_id()
        # Only an agent that has depth left to launch is told about its sub-agents, and given the tools.
        can_launch = bool(agent.subagents) and depth < self.max_depth
        system_prompt = agent.build_system_prompt(can_launch=can_launch, can_background=self._can_background(agent))
        prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()
        runner_tool_names = self._runner_tool_names(agent, can_launch)
        message = agent_input.model_dump_json(indent=2)
        result_cls = AgentResult[agent.output_model]  # ty: ignore[invalid-type-form]  # the output model is known only now
        started = time.perf_counter()

        logger.info(
            "invocation_started invocation_id=%s agent=%s version=%s model=%s reasoning=%s job_id=%s max_attempts=%d",
            invocation_id,
            agent.name,
            agent.version,
            agent.model.model,
            agent.model.label(),
            job_id,
            self.max_attempts,
        )

        declaration_hash = agent_hash(agent)
        if depth == 1 and job_id is not None:  # only runs of the pipeline resume; a launched agent is not looked up
            reused = await self._reuse(agent, agent_input, declaration_hash, job_id, invocation_id, result_cls)
            if reused is not None:
                return reused

        agent_run_id = ""
        error = ""
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            agent_run_id = new_id()
            # This attempt's tools: the agent's own, and those of its MCP servers as the agent declares them. The
            # servers are started here, before the attempt waits for a slot, so that a slow start holds none. A failure
            # is raised inside the attempt below, so it is logged and counted like any other.
            registry = ToolRegistry()
            setup_error: Exception | None = None
            try:
                for local_tool in agent.tools:
                    registry.add(local_tool, "local")
                for server in agent.mcp_servers:
                    await registry.add_mcp(server, lease)
            except Exception as exc:
                setup_error = exc
            await self.db.create_run(
                RunStart(
                    agent_run_id=agent_run_id,
                    invocation_id=invocation_id,
                    attempt=attempt,
                    job_id=job_id,
                    job_execution=_job_execution(job_id),
                    parent_agent_run_id=parent_agent_run_id,
                    depth=depth,
                    agent_name=agent.name,
                    agent_version=agent.version,
                    agent_hash=declaration_hash,
                    system_prompt_hash=prompt_hash,
                    tools_hash=tools_hash(registry.tools, runner_tool_names),
                    provider=agent.model.provider,
                    model_name=agent.model.model,
                    reasoning=agent.model.label(),
                    settings=agent.model.model_dump(mode="json"),
                    input_hash=input_hash(agent_input),
                )
            )
            scope: Entered | None = None  # the observers' scope of this attempt, once it runs
            attempt_end: AttemptEnd | None = None
            # The attempt row stays INIT while it waits for a slot, and is RUNNING only once it holds one.
            queued = self._slots.locked()
            if queued:
                logger.info("waiting_for_slot agent_run_id=%s limit=%d", agent_run_id, self.max_concurrent)
            wait_started = time.perf_counter()
            slot = _Slot(self._slots)
            await slot.acquire()
            try:
                if queued:
                    logger.info(
                        "slot_acquired agent_run_id=%s waited_ms=%.0f",
                        agent_run_id,
                        (time.perf_counter() - wait_started) * 1000,
                    )
                await self.db.mark_run_running(agent_run_id)
                logger.info(
                    "attempt_started agent_run_id=%s invocation_id=%s attempt=%d/%d tools=%s",
                    agent_run_id,
                    invocation_id,
                    attempt,
                    self.max_attempts,
                    ",".join(
                        registry.names + runner_tool_names
                    ),  # in the log only: the run log keeps a hash of the tools
                )
                attempt_started = time.perf_counter()
                if self._observers:
                    scope = await self._observers.enter(
                        "attempt",
                        AttemptStart(
                            agent_run_id=agent_run_id,
                            invocation_id=invocation_id,
                            attempt=attempt,
                            max_attempts=self.max_attempts,
                            job_id=job_id,
                            parent_agent_run_id=parent_agent_run_id,
                            parent_tool_call_id=parent_tool_call_id,
                            depth=depth,
                            agent_name=agent.name,
                            agent_version=agent.version,
                            agent_description=agent.description,
                            agent_hash=declaration_hash,
                            provider=agent.model.provider,
                            model=agent.model.model,
                            input=agent_input.model_dump(mode="json") if self._observers.wants_content else None,
                            system_prompt=system_prompt if self._observers.wants_content else None,
                        ),
                    )

                try:
                    if setup_error is not None:
                        raise setup_error
                    context = RunContext(store=self.store, job_id=job_id, agent_run_id=agent_run_id)
                    output, input_tokens, output_tokens, reported_model = await self._attempt(
                        agent, agent_input, system_prompt, message, context, slot, depth, can_launch, registry
                    )
                except asyncio.CancelledError:
                    # The task that awaits this run was cancelled (a streaming agent that was stopped, a caller that
                    # gave up): the row says so instead of staying RUNNING, and its background children are cancelled.
                    await self._cancel_children(agent_run_id)
                    await self.db.finish_run(
                        agent_run_id,
                        RunEnd(status=RunStatus.CANCELLED, latency_ms=(time.perf_counter() - attempt_started) * 1000),
                    )
                    attempt_end = AttemptEnd(
                        status=RunStatus.CANCELLED,
                        final=True,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000,
                    )
                    logger.warning(
                        "attempt_cancelled agent_run_id=%s attempt=%d/%d", agent_run_id, attempt, self.max_attempts
                    )
                    raise
                except Exception as exc:
                    error = _format_error(exc)
                    non_retryable = is_non_retryable(exc)
                    error_type, error_digest = error_fields(
                        exc
                    )  # the text goes to the logs and the result, not the run log
                    await self.db.finish_run(
                        agent_run_id,
                        RunEnd(
                            status=RunStatus.FAILED,
                            error_type=error_type,
                            error_hash=error_digest,
                            latency_ms=(time.perf_counter() - attempt_started) * 1000,
                        ),
                    )
                    attempt_end = AttemptEnd(
                        status=RunStatus.FAILED,
                        final=non_retryable or attempt >= self.max_attempts,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000,
                        error_type=error_type,
                        error=error if self._observers.wants_content else None,
                    )
                    logger.warning(
                        "attempt_failed agent_run_id=%s attempt=%d/%d non_retryable=%s error=%s",
                        agent_run_id,
                        attempt,
                        self.max_attempts,
                        non_retryable,
                        error,
                    )
                else:
                    await self.db.finish_run(
                        agent_run_id,
                        RunEnd(
                            status=RunStatus.SUCCESS,
                            output_hash=output_hash(output),
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            latency_ms=(time.perf_counter() - attempt_started) * 1000,
                            reported_model_name=reported_model,
                        ),
                    )
                    attempt_end = AttemptEnd(
                        status=RunStatus.SUCCESS,
                        final=True,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        reported_model=reported_model,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000,
                        output=output.model_dump(mode="json") if self._observers.wants_content else None,
                    )
                    if depth == 1 and job_id is not None:  # only a step of a pipeline can be resumed
                        await self._keep_result(job_id, declaration_hash, input_hash(agent_input), output)
                    latency_ms = (time.perf_counter() - started) * 1000
                    logger.info(
                        "invocation_finished invocation_id=%s status=SUCCESS attempts=%d input_tokens=%d output_tokens=%d latency_ms=%.0f",
                        invocation_id,
                        attempt,
                        input_tokens,
                        output_tokens,
                        latency_ms,
                    )
                    return result_cls(
                        status=RunStatus.SUCCESS,
                        output=output,
                        agent_run_id=agent_run_id,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        attempts=attempt,
                        latency_ms=latency_ms,
                        model_name=reported_model,
                    )
            finally:
                self._children.pop(agent_run_id, None)  # whatever it started carries on, unless it was cancelled above
                if scope is not None:
                    await scope.leave(attempt_end)
                slot.release()

            # The slot is released here, so a retry delay never holds one.
            if non_retryable or attempt >= self.max_attempts:
                break
            logger.info("retry_waiting invocation_id=%s delay_s=%s", invocation_id, self.retry_delay_s)
            await asyncio.sleep(self.retry_delay_s)

        latency_ms = (time.perf_counter() - started) * 1000
        logger.error(
            "invocation_finished invocation_id=%s status=FAILED attempts=%d latency_ms=%.0f error=%s",
            invocation_id,
            attempt,
            latency_ms,
            error,
        )
        return result_cls(
            status=RunStatus.FAILED,
            error=error,
            agent_run_id=agent_run_id,
            attempts=attempt,
            latency_ms=latency_ms,
        )

    async def _reuse(
        self,
        agent: AgentBase,
        agent_input: AgentInput,
        declaration_hash: str,
        job_id: str,
        invocation_id: str,
        result_cls: type[AgentResult[Any]],
    ) -> AgentResult[Any] | None:
        """Answer from a run that succeeded with this agent and input, in the job it resumes, or in an earlier execution of this
        same job (a takeover).

        The stored output must still validate against the output model and pass `check_output`; if not, the agent runs
        as usual. The answer is written as a run of its own in this job, with no tokens, and takes no slot.

        There are two sources, with one mechanism. A job that **resumes another** looks in the old job (all of its runs). A job that a
        worker **took over** (`executions` above 1, and resumable) looks in **itself**, at the runs of earlier executions only, so that a
        run never reuses its own execution's rows; its results are already under its own key, so nothing is copied forward. A taken-over
        job that also resumes another looks in itself first."""
        job = await self.db.get_job(job_id)
        if job is None:
            return None
        sources: list[tuple[str, int | None]] = []  # (the job to look in, only the runs before this execution)
        execution = (
            _job_execution(job_id) or job.executions
        )  # the one that asks: not the row's, if this worker lost the job
        if job.resumable and execution > 1:
            sources.append((job_id, execution))
        if job.resumed_from is not None:
            sources.append((job.resumed_from, None))
        if not sources:
            return None
        if self.results is None:
            if job.resumed_from is not None:
                raise SetupError(
                    f"Job {job_id} resumes {job.resumed_from}, which needs the outputs of its steps, and this runner has no "
                    "result store: pass `results=` to AgentRunner (the default keeps them in the run log's sqlite file)"
                )
            return None  # a takeover with no store kept nothing: the job runs again
        this_input_hash = input_hash(agent_input)
        old: AgentRunRow | None = None
        source_job = ""
        for source_job, before in sources:
            old = await self.db.find_reusable_run(
                source_job, declaration_hash, this_input_hash, before
            )  # did the step succeed
            if old is not None:
                break
        if old is None:
            return None
        key = ResultKey(job_id=source_job, agent_hash=declaration_hash, input_hash=this_input_hash)
        stored = await self.results.get(key)  # and what was its output
        if stored is None:
            logger.warning("result_missing reused_from=%s agent=%s: the agent runs", old.agent_run_id, agent.name)
            return None
        try:
            output = agent.output_model.model_validate(stored)
            agent.check_output(agent_input, output)
        except Exception as exc:
            logger.warning(
                "run_reuse_rejected reused_from=%s agent=%s reason=%s", old.agent_run_id, agent.name, _format_error(exc)
            )
            return None
        agent_run_id = await self.db.create_run(
            RunStart(
                agent_run_id=new_id(),
                invocation_id=invocation_id,
                attempt=1,
                job_id=job_id,
                job_execution=_job_execution(job_id),
                agent_name=agent.name,
                agent_version=agent.version,
                agent_hash=declaration_hash,
                system_prompt_hash=old.system_prompt_hash,
                tools_hash=old.tools_hash,
                provider=agent.model.provider,
                model_name=agent.model.model,
                reasoning=agent.model.label(),
                settings=agent.model.model_dump(mode="json"),
                input_hash=this_input_hash,
                reused_from_agent_run_id=old.agent_run_id,
            )
        )
        await self.db.finish_run(
            agent_run_id,
            RunEnd(
                status=RunStatus.SUCCESS,
                output_hash=output_hash(output),
                input_tokens=0,
                output_tokens=0,
                latency_ms=0.0,
                reported_model_name=old.reported_model_name,
            ),
        )
        if source_job != job_id:  # a takeover found it under this job's own key already
            await self._keep_result(
                job_id, declaration_hash, this_input_hash, output
            )  # a later attempt resumes from this job too
        logger.info("run_reused agent_run_id=%s reused_from=%s agent=%s", agent_run_id, old.agent_run_id, agent.name)
        if self._observers:
            await self._observers.emit(
                "reused",
                ReusedRun(
                    agent_run_id=agent_run_id,
                    invocation_id=invocation_id,
                    reused_from_agent_run_id=old.agent_run_id,
                    resumed_from=job.resumed_from,
                    job_id=job_id,
                    agent_name=agent.name,
                    agent_version=agent.version,
                    agent_hash=declaration_hash,
                    output=output.model_dump(mode="json") if self._observers.wants_content else None,
                ),
            )
        return result_cls(
            status=RunStatus.SUCCESS,
            output=output,
            agent_run_id=agent_run_id,
            model_name=old.reported_model_name,
            reused=True,
        )

    async def _keep_result(self, job_id: str, declaration_hash: str, this_input_hash: str, output: AgentOutput) -> None:
        """Put the output of a step in the result store, if the job is resumable, so that a later job can resume from it. A
        store that fails does not fail the run, which succeeded: the step just cannot be reused."""
        if self.results is None:
            return
        try:
            job = await self.db.get_job(job_id)
            if job is None or not job.resumable:
                return
            key = ResultKey(job_id=job_id, agent_hash=declaration_hash, input_hash=this_input_hash)
            await self.results.put(key, output.model_dump(mode="json"))
        except Exception as exc:
            logger.warning(
                "result_not_kept job_id=%s agent_hash=%s reason=%s", job_id, declaration_hash[:12], _format_error(exc)
            )

    async def run_many(
        self,
        agent_cls: type[AgentBase],
        agent_inputs: Sequence[AgentInput],
        *,
        job_id: str | None = None,
    ) -> list[AgentResult[Any]]:
        """Run one agent over many inputs concurrently (fan-out).

        Results come back in input order. As with `run`, runtime failures are FAILED results, not
        exceptions, so the caller decides what a failure means. There is no limit of its own: the
        runner's concurrency limit bounds how many attempts run at once.
        """
        name = agent_cls().name  # ty: ignore[missing-argument]  # a subclass supplies the defaults
        logger.info("fan_out_started agent=%s count=%d job_id=%s", name, len(agent_inputs), job_id)
        results = list(
            await asyncio.gather(*(self.run(agent_cls, agent_input, job_id=job_id) for agent_input in agent_inputs))
        )
        failed = sum(result.status == RunStatus.FAILED for result in results)
        log = logger.warning if failed else logger.info
        log("fan_out_finished agent=%s count=%d failed=%d job_id=%s", name, len(results), failed, job_id)
        return results

    def _launch_tools(
        self, agent: AgentBase, context: RunContext, slot: _Slot, depth: int, can_launch: bool = True
    ) -> list[BaseTool]:
        """`launch_agent`, `start_background` and `get_agent_schema` for one attempt of `agent`: they know the agents it may
        launch and start, and count its launches, so they are built for each attempt. `launch_agent` is built when the agent
        may launch (`can_launch`), `start_background` when it may start background agents here.

        All return text, also for a refusal or a failed child, so that the model can read what went wrong and carry
        on; they never raise into the run.
        """
        launches = 0
        started: list[
            str
        ] = []  # the ids of the background jobs of this attempt that were started and may still be running
        with_background = self._can_background(agent)
        allowed = ", ".join(sub().name for sub in agent.subagents)
        usable = ", ".join(
            dict.fromkeys([*(sub().name for sub in agent.subagents), *(entry_name(e) for e in agent.background_agents)])
        )

        def unknown(name: str) -> str:
            return f"'{name}' is not an agent you can launch. You can launch: {allowed}."

        @tool
        async def get_agent_schema(agent_name: str) -> str:
            """The input an agent you can launch or start in the background expects (a JSON schema), and the output it returns,
            as JSON."""
            child = agent.subagent_instance(agent_name)
            entry = agent.background_entry(agent_name) if with_background else None
            if child is None and entry is None:
                if with_background:
                    return f"'{agent_name}' is not an agent you can use. You can use: {usable}."
                return unknown(agent_name)
            if child is not None:
                description, input_model, output_model = child.description, child.input_model, child.output_model
            else:
                assert entry is not None
                declared = _as_job(entry)
                description = entry_description(entry)
                input_model, output_model = declared.input_model, declared.output_model
            return json.dumps(
                {
                    "agent": agent_name,
                    "description": description,
                    "input_schema": input_model.model_json_schema(),
                    "output_schema": output_model.model_json_schema(),
                }
            )

        @tool
        async def launch_agent(agent_name: str, input_json: str) -> str:
            """Launch an agent you can launch and wait for it. `input_json` is its input, as a JSON string that
            follows its input schema (see `get_agent_schema`). The result is the agent's output as JSON, or an error."""
            nonlocal launches
            child = agent.subagent_instance(agent_name)
            if child is None:
                return unknown(agent_name)
            if depth >= self.max_depth:  # the tool is not offered at this depth; this is for a call made anyway
                return f"You cannot launch agents here: the limit of {self.max_depth} nested agents is reached."
            if launches >= self.max_launches:
                return f"You cannot launch more agents in this run: the limit is {self.max_launches} launches."
            try:
                child_input = child.input_model.model_validate_json(input_json)
            except ValidationError as exc:
                return f"The input for '{agent_name}' is not valid: {exc}. Check `get_agent_schema` and try again."
            launches += 1
            logger.info(
                "agent_launch parent_agent_run_id=%s child=%s depth=%d launch=%d/%d job_id=%s",
                context.agent_run_id,
                agent_name,
                depth + 1,
                launches,
                self.max_launches,
                context.job_id,
            )
            slot.leave()  # the child needs a slot of its own
            try:
                result = await self._run(
                    type(child),
                    child_input,
                    job_id=context.job_id,
                    parent_agent_run_id=context.agent_run_id,
                    parent_tool_call_id=CURRENT_TOOL_CALL_ID.get(),  # set by the observers' callback; None without one
                    depth=depth + 1,
                )
            finally:
                await slot.back()
            if result.status == RunStatus.SUCCESS and result.output is not None:
                return json.dumps(
                    {"status": "SUCCESS", "agent": agent_name, "output": result.output.model_dump(mode="json")}
                )
            return json.dumps({"status": "FAILED", "agent": agent_name, "error": result.error})

        @tool
        async def start_background(agent_name: str, input_json: str) -> str:
            """Start an agent you can start in the background, and carry on: this returns at once with the id of its job. Its
            result is given to you in a later message. `input_json` is its input, as a JSON string that follows its input
            schema (see `get_agent_schema`)."""
            entry = agent.background_entry(agent_name)
            if entry is None:
                names = ", ".join(entry_name(e) for e in agent.background_agents)
                return f"'{agent_name}' is not an agent you can start in the background. You can start: {names}."
            if await self._live_background(started) >= self.max_background:
                return (
                    f"You cannot start more agents in the background now: {self.max_background} are still running. "
                    "Carry on, and try again when one has finished."
                )
            declared = _as_job(entry)
            try:
                child_input = declared.input_model.model_validate_json(input_json)
            except ValidationError as exc:
                return f"The input for '{agent_name}' is not valid: {exc}. Check `get_agent_schema` and try again."
            try:
                job_id = await self._start_background(entry, declared, child_input, context)
            except Exception as exc:  # a refusing queue, a database that fails: the model reads it, the run goes on
                logger.exception(
                    "background_start_failed agent=%s parent_agent_run_id=%s", agent_name, context.agent_run_id
                )
                return f"'{agent_name}' could not be started ({type(exc).__name__}). Carry on without it."
            started.append(job_id)
            return json.dumps({"status": "STARTED", "agent": agent_name, "job_id": job_id})

        tools: list[BaseTool] = [launch_agent] if can_launch else []
        if with_background:
            tools.append(start_background)
        tools.append(get_agent_schema)
        return tools

    async def _live_background(self, job_ids: list[str]) -> int:
        """How many of these background jobs are not final. The ones that are final are dropped from the list."""
        db = self.background.db if self.background is not None else self.db
        live: list[str] = []
        for job_id in job_ids:
            row = await db.get_job(job_id)
            if row is not None and row.status not in FINAL:
                live.append(job_id)
        job_ids[:] = live
        return len(live)

    async def _start_background(self, entry: Any, declared: Any, child_input: Any, context: RunContext) -> str:
        """Start the child as a job of its own and return its id: on a worker, through the client, or here, as a task."""
        parent = context.agent_run_id
        tool_call_id = CURRENT_TOOL_CALL_ID.get()  # set by the callback of the tool that is running: for the trace only
        task: asyncio.Task[Any] | None = None
        if self.background is not None:
            job_id = await self.background.submit(
                entry, child_input, parent_agent_run_id=parent, parent_tool_call_id=tool_call_id
            )
            placement = "worker"
        else:
            job_id = await self.db.create_job(
                declared.name,
                job_version=declared.version,
                job_hash=declared.job_hash(),
                input_hash=input_hash(child_input),
                parent_agent_run_id=parent,
            )
            trace_context: dict[str, str] = {}
            if self._observers:  # the carrier of the tool call's span: nothing about a trace is stored
                trace_context = await self._observers.submitted(
                    JobSubmitted(
                        job_id=job_id,
                        name=declared.name,
                        version=declared.version,
                        job_hash=declared.job_hash(),
                        input_hash=input_hash(child_input),
                        parent_agent_run_id=parent,
                        parent_tool_call_id=tool_call_id,
                        input=child_input.model_dump(mode="json") if self._observers.wants_content else None,
                    )
                )
            task = asyncio.create_task(self._run_background(declared, job_id, child_input, trace_context))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            placement = "here"
        self._children.setdefault(parent, []).append(_Child(job_id, declared.name, task))
        logger.info(
            "background_started job_id=%s agent=%s placement=%s parent_agent_run_id=%s",
            job_id,
            declared.name,
            placement,
            parent,
        )
        self._announce_background(parent, job_id, declared.name)
        return job_id

    async def _run_background(
        self, declared: Any, job_id: str, child_input: Any, trace_context: dict[str, str] | None = None
    ) -> None:
        """The task of a child that runs here: the same code as `run_job`. What it raises is logged, and nothing more."""
        from featherweight.jobs import run_job  # jobs.py imports this module

        try:
            await run_job(self, declared, child_input, job_id=job_id, trace_context=trace_context or {})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background_failed job_id=%s", job_id)

    def _announce_background(self, run_id: str, job_id: str, agent: str) -> None:
        """On the stream of a chat run that is open: `background_started` now, and `background_finished` when the child's
        job is final. Metadata only: the application reads the result itself, from its store."""
        emit = self._emitters.get(run_id)
        if emit is None:
            return  # not a streamed run, or its stream has ended
        emit(CustomEvent(name="background_started", value={"job_id": job_id, "agent": agent}))
        watcher = asyncio.create_task(self._watch_background(run_id, job_id, agent))
        self._watchers.setdefault(run_id, []).append(watcher)

    async def _watch_background(self, run_id: str, job_id: str, agent: str) -> None:
        from featherweight.events import wait_for_job  # events.py imports the database, as this module does

        db = self.background.db if self.background is not None else self.db
        try:
            row = await wait_for_job(db, job_id)
        except Exception:
            logger.exception("background_watch_failed job_id=%s", job_id)
            return
        emit = self._emitters.get(run_id)
        if emit is not None:  # the stream is still open; after it ended the application follows the job's own events
            emit(
                CustomEvent(
                    name="background_finished",
                    value={"job_id": job_id, "agent": agent, "status": row.status.value, "error_type": row.error_type},
                )
            )

    async def _close_background_events(self, run_id: str) -> None:
        """The stream of a chat run is ending: no more events, and no more watching. The children carry on."""
        self._emitters.pop(run_id, None)
        watchers = self._watchers.pop(run_id, [])
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)

    async def _cancel_children(self, run_id: str) -> None:
        """Cancel the background children of a run that was stopped or cancelled and that are still live: the task of a child
        that runs here (its job ends `CANCELLED`), the job of a child on a worker (`cancel_requested`)."""
        children = self._children.pop(run_id, [])
        tasks: list[asyncio.Task[Any]] = []
        for child in children:
            if child.task is not None:
                if not child.task.done():
                    child.task.cancel()
                    tasks.append(child.task)
            elif self.background is not None:
                try:
                    await self.background.cancel(child.job_id)
                except Exception:
                    logger.warning("background_cancel_failed job_id=%s", child.job_id, exc_info=True)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for child in children:
            if child.task is not None:  # cancelled before it ran: the job never started, so it is still queued
                await self.db.cancel_queued_job(child.job_id)
        if children:
            logger.info("background_cancelled parent_agent_run_id=%s children=%d", run_id, len(children))

    @staticmethod
    def _skill_tools(agent: AgentBase) -> list[BaseTool]:
        """`get_skill` for `agent`: it reads only the skills the agent lists, and says so when asked for another."""
        by_name = {skill.name: skill for skill in agent.skills}
        available = ", ".join(by_name)

        @tool
        async def get_skill(name: str, resource: str = "") -> str:
            """Read one of your skills: its instructions, and the other files it has. Pass `resource` (a path the
            instructions or this tool named) to read one of those files instead."""
            skill = by_name.get(name)
            if skill is None:
                return f"'{name}' is not one of your skills. Your skills: {available}."
            logger.info("skill_read agent=%s skill=%s resource=%s", agent.name, name, resource or "-")
            return skill.read(resource)

        return [get_skill]

    async def _attempt(
        self,
        agent: AgentBase,
        agent_input: AgentInput,
        system_prompt: str,
        message: str,
        context: RunContext,
        slot: _Slot,
        depth: int,
        can_launch: bool,
        registry: ToolRegistry,
    ) -> tuple[AgentOutput, int, int, str | None]:
        try:
            model = agent.model.build()  # the agent's own settings build its chat model
        except Exception as exc:
            raise ModelSetupError(str(exc)) from exc

        self._add_runner_tools(agent, context, slot, depth, can_launch, registry)
        lc_agent = create_agent(
            model,
            tools=registry.tools,  # only the allow-listed tools are ever bound
            system_prompt=system_prompt,
            response_format=agent.output_model,
            context_schema=RunContext,
        )
        token = CURRENT_AGENT_RUN_ID.set(context.agent_run_id)
        try:
            result = await lc_agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config={
                    "recursion_limit": agent.recursion_limit,
                    "callbacks": self._observers.callbacks(
                        context.agent_run_id, agent.model.provider, agent.model.model
                    ),
                },
                context=context,  # reaches tools through ToolRuntime; the model never sees it
            )
        finally:
            CURRENT_AGENT_RUN_ID.reset(token)

        structured = result.get("structured_response")
        if structured is None:
            raise RuntimeError("The agent finished without a structured response")
        output = agent.output_model.model_validate(
            structured.model_dump() if hasattr(structured, "model_dump") else structured
        )
        agent.check_output(agent_input, output)  # may raise ValueError: a failed attempt, retried
        input_tokens, output_tokens, reported_model = _usage(result["messages"])
        return output, input_tokens, output_tokens, reported_model
