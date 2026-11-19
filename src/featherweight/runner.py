"""AgentRunner: runs an agent definition through langgraph, validates the output, retries, and logs."""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal

from ag_ui.core import (
    BaseEvent,
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

from featherweight.context import CURRENT_AGENT_RUN_ID, RunContext
from featherweight.db import Database, new_id
from featherweight.discovery import discovery_tools
from featherweight.errors import RETRYABLE_4XX, SetupError
from featherweight.fingerprint import agent_hash, input_hash
from featherweight.mcp_connections import Lease, MCPConnections
from featherweight.messages import to_langchain
from featherweight.models import (
    DISCOVERY_TOOL_NAMES,
    LAUNCH_TOOL_NAMES,
    SKILL_TOOL_NAMES,
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentResult,
    RunStatus,
)
from featherweight.registry import ToolRegistry
from featherweight.results import ResultKey, ResultStore, SqliteResultStore
from featherweight.store import RunStore
from featherweight.stream_events import EventMapper
from featherweight.streaming import ChatInput, StreamingAgent, StreamingRun

logger = logging.getLogger(__name__)

ENV_MAX_CONCURRENT = "MAX_CONCURRENT_AGENT_RUNS"
DEFAULT_MAX_CONCURRENT = 10
# How far agents may launch agents: the most agents in one chain (A launches B launches C is 3), and the most launches
# one attempt of one agent may make. A depth of 1 turns launching off.
ENV_MAX_DEPTH = "MAX_AGENT_DEPTH"
DEFAULT_MAX_DEPTH = 3
ENV_MAX_LAUNCHES = "MAX_AGENT_LAUNCHES"
DEFAULT_MAX_LAUNCHES = 10

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
        self.output_length = 0
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
        results: ResultStore | None | Literal["default"] = "default",
    ):
        """`store` is what the `store_get` / `store_set` tools read and write (default: a new RunStore).

        `max_concurrent` caps how many attempts (model calls) run at once across this runner. Default:
        `MAX_CONCURRENT_AGENT_RUNS` from the environment, else 10.

        `max_depth` is the most agents in one chain of launches, the first agent included (A launches B launches C is
        3). Default: `MAX_AGENT_DEPTH`, else 3. At 1 no agent can launch another. `max_launches` is the most launches
        one attempt of one agent may make. Default: `MAX_AGENT_LAUNCHES`, else 10.

        `results` is where the outputs that resuming a job reads back are kept (a `ResultStore`), for the jobs created
        `resumable=True`. The default is a sqlite store in the run log's file. `None` keeps nothing, and then a job that resumes
        another is a `SetupError`.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_concurrent is None:
            max_concurrent = max_concurrent_from_env()
        elif max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        for name, value in (("max_depth", max_depth), ("max_launches", max_launches)):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1")
        self.max_depth = max_depth if max_depth is not None else max_depth_from_env()
        self.max_launches = max_launches if max_launches is not None else max_launches_from_env()
        self.max_concurrent = max_concurrent
        self._slots = asyncio.Semaphore(max_concurrent)
        self.store = store or RunStore()
        self.db = db
        self.results: ResultStore | None = SqliteResultStore(db) if isinstance(results, str) else results
        self.max_attempts = max_attempts
        self.retry_delay_s = retry_delay_s
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
        system_prompt = agent.build_system_prompt(can_launch=can_launch)
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
                agent_run_id=run.run_id,
                invocation_id=new_id(),
                attempt=1,
                job_id=job_id,
                agent_name=agent.name,
                agent_version=agent.version,
                model_name=agent.model.model,
                reasoning=agent.model.label(),
                provider=agent.model.provider,
                settings=agent.model.model_dump_json(),
                tool_names=registry.names + self._runner_tool_names(agent, can_launch),
                system_prompt_hash=hashlib.sha256(system_prompt.encode()).hexdigest(),
                agent_hash=agent_hash(agent),
                input_hash=input_hash(chat_input),
                input={"messages": len(chat_input.messages)},  # the words stay with the application
                thread_id=chat_input.thread_id,
            )
            self._streams[run.run_id] = run
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
            outcome, error = "success", None
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
                outcome, error = "failed", _format_error(exc)
            finally:
                run._interrupt = None
            await self._end_stream(run, turn, emit, outcome, error, agent, thread_id, started)
            if outcome == "left":
                raise asyncio.CancelledError
        finally:
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
                    turn.output_length += len(event.delta)
                    turn.output_digest.update(event.delta.encode())
        finally:
            slot.release()

    async def _end_stream(
        self,
        run: StreamingRun,
        turn: "_Turn",
        emit: Callable[[BaseEvent], None],
        outcome: str,
        error: str | None,
        agent: StreamingAgent,
        thread_id: str,
        started: float,
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
        run.status, run.error, run.usage = status, error, usage
        if outcome != "left":
            for event in closing:
                emit(event)
            if outcome == "failed":
                emit(RunErrorEvent(message=error or ""))
            else:
                finished = RunFinishedCancelledOutcome() if outcome == "cancelled" else RunFinishedSuccessOutcome()
                emit(RunFinishedEvent(thread_id=thread_id, run_id=run.run_id, outcome=finished, usage=[usage]))
        await self.db.finish_run(
            run.run_id,
            status=status,
            error=error,
            output={"chars": turn.output_length},
            input_tokens=mapper.input_tokens,
            output_tokens=mapper.output_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
            output_hash=turn.output_digest.hexdigest(),
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
            config={"recursion_limit": agent.recursion_limit},
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
    def _runner_tool_names(agent: AgentBase, can_launch: bool) -> list[str]:
        """The names of the tools the runner gives `agent`, by what it declares."""
        return (
            (list(LAUNCH_TOOL_NAMES) if can_launch else [])
            + (list(SKILL_TOOL_NAMES) if agent.skills else [])
            + (list(DISCOVERY_TOOL_NAMES) if agent.has_discovery else [])
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
            (self._launch_tools(agent, context, slot, depth) if can_launch else [])
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
        depth: int = 1,
    ) -> AgentResult[Any]:
        """`run`, for the agents launched by other agents too: `depth` is 1 for a top-level run, and the child shares
        the job and records the attempt that launched it. The run holds a lease on its MCP servers from its first
        attempt to its last, so that a retry does not start them again, and lets go of them when it is done."""
        lease = Lease(self._connections)
        try:
            return await self._invoke(
                agent_cls, agent_input, lease, job_id=job_id, parent_agent_run_id=parent_agent_run_id, depth=depth
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
        depth: int,
    ) -> AgentResult[Any]:
        agent = agent_cls()  # ty: ignore[missing-argument]  # a subclass supplies the defaults
        if not isinstance(agent_input, agent.input_model):
            raise TypeError(f"{agent.name} expects {agent.input_model.__name__}, got {type(agent_input).__name__}")

        invocation_id = new_id()
        # Only an agent that has depth left to launch is told about its sub-agents, and given the tools.
        can_launch = bool(agent.subagents) and depth < self.max_depth
        system_prompt = agent.build_system_prompt(can_launch=can_launch)
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
                agent_run_id=agent_run_id,
                invocation_id=invocation_id,
                attempt=attempt,
                job_id=job_id,
                agent_name=agent.name,
                agent_version=agent.version,
                model_name=agent.model.model,
                reasoning=agent.model.label(),
                provider=agent.model.provider,
                settings=agent.model.model_dump_json(),
                tool_names=registry.names + runner_tool_names,
                system_prompt_hash=prompt_hash,
                agent_hash=declaration_hash,
                input_hash=input_hash(agent_input),
                input=agent_input,
                parent_agent_run_id=parent_agent_run_id,
                depth=depth,
            )
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
                    "attempt_started agent_run_id=%s invocation_id=%s attempt=%d/%d",
                    agent_run_id,
                    invocation_id,
                    attempt,
                    self.max_attempts,
                )
                attempt_started = time.perf_counter()

                try:
                    if setup_error is not None:
                        raise setup_error
                    context = RunContext(store=self.store, job_id=job_id, agent_run_id=agent_run_id)
                    output, input_tokens, output_tokens, reported_model = await self._attempt(
                        agent, agent_input, system_prompt, message, context, slot, depth, can_launch, registry
                    )
                except asyncio.CancelledError:
                    # The task that awaits this run was cancelled (a streaming agent that was stopped, a caller that
                    # gave up): the row says so instead of staying RUNNING.
                    await self.db.finish_run(
                        agent_run_id,
                        status=RunStatus.CANCELLED,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000,
                    )
                    logger.warning(
                        "attempt_cancelled agent_run_id=%s attempt=%d/%d", agent_run_id, attempt, self.max_attempts
                    )
                    raise
                except Exception as exc:
                    error = _format_error(exc)
                    non_retryable = is_non_retryable(exc)
                    await self.db.finish_run(
                        agent_run_id,
                        status=RunStatus.FAILED,
                        error=error,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000,
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
                        status=RunStatus.SUCCESS,
                        output=output,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=(time.perf_counter() - attempt_started) * 1000,
                        reported_model_name=reported_model,
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
        """In a job that resumes another, answer from a run of the old job that succeeded with this agent and input.

        The stored output must still validate against the output model and pass `check_output`; if not, the agent runs
        as usual. The answer is written as a run of its own in this job, with no tokens, and takes no slot."""
        job = await self.db.get_job(job_id)
        if job is None or job.resumed_from is None:
            return None
        if self.results is None:
            raise SetupError(
                f"Job {job_id} resumes {job.resumed_from}, which needs the outputs of its steps, and this runner has no "
                "result store: pass `results=` to AgentRunner (the default keeps them in the run log's sqlite file)"
            )
        this_input_hash = input_hash(agent_input)
        old = await self.db.find_reusable_run(
            job.resumed_from, declaration_hash, this_input_hash
        )  # did the step succeed
        if old is None:
            return None
        key = ResultKey(job_id=job.resumed_from, agent_hash=declaration_hash, input_hash=this_input_hash)
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
            agent_run_id=new_id(),
            invocation_id=invocation_id,
            attempt=1,
            job_id=job_id,
            agent_name=agent.name,
            agent_version=agent.version,
            model_name=agent.model.model,
            reasoning=agent.model.label(),
            provider=agent.model.provider,
            settings=agent.model.model_dump_json(),
            tool_names=old.tool_names,
            system_prompt_hash=old.system_prompt_hash,
            agent_hash=declaration_hash,
            input_hash=this_input_hash,
            input=agent_input,
            reused_from_agent_run_id=old.agent_run_id,
        )
        await self.db.finish_run(
            agent_run_id,
            status=RunStatus.SUCCESS,
            output=output,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0.0,
            reported_model_name=old.reported_model_name,
        )
        await self._keep_result(
            job_id, declaration_hash, this_input_hash, output
        )  # a later attempt resumes from this job too
        logger.info("run_reused agent_run_id=%s reused_from=%s agent=%s", agent_run_id, old.agent_run_id, agent.name)
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

    def _launch_tools(self, agent: AgentBase, context: RunContext, slot: _Slot, depth: int) -> list[BaseTool]:
        """`launch_agent` and `get_agent_schema` for one attempt of `agent`: they know the agents it may launch, and
        count its launches, so they are built for each attempt.

        Both return text, also for a refusal or a failed child, so that the model can read what went wrong and carry
        on; they never raise into the run.
        """
        launches = 0
        allowed = ", ".join(sub().name for sub in agent.subagents)

        def unknown(name: str) -> str:
            return f"'{name}' is not an agent you can launch. You can launch: {allowed}."

        @tool
        async def get_agent_schema(agent_name: str) -> str:
            """The input an agent you can launch expects (a JSON schema), and the output it returns, as JSON."""
            child = agent.subagent_instance(agent_name)
            if child is None:
                return unknown(agent_name)
            return json.dumps(
                {
                    "agent": child.name,
                    "description": child.description,
                    "input_schema": child.input_model.model_json_schema(),
                    "output_schema": child.output_model.model_json_schema(),
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
                    depth=depth + 1,
                )
            finally:
                await slot.back()
            if result.status == RunStatus.SUCCESS and result.output is not None:
                return json.dumps(
                    {"status": "SUCCESS", "agent": agent_name, "output": result.output.model_dump(mode="json")}
                )
            return json.dumps({"status": "FAILED", "agent": agent_name, "error": result.error})

        return [launch_agent, get_agent_schema]

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
                config={"recursion_limit": agent.recursion_limit},
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
