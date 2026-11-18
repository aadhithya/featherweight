"""AgentRunner: runs an agent definition through langgraph, validates the output, retries, and logs."""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import ValidationError

from micro.context import RunContext
from micro.db import Database, new_id
from micro.models import AgentBase, AgentInput, AgentOutput, AgentResult, RunStatus
from micro.store import RunStore

logger = logging.getLogger(__name__)

ModelFactory = Callable[[AgentBase], BaseChatModel]

ENV_MAX_CONCURRENT = "MAX_CONCURRENT_AGENT_RUNS"
DEFAULT_MAX_CONCURRENT = 10
# How far agents may launch agents: the most agents in one chain (A launches B launches C is 3), and the most launches
# one attempt of one agent may make. A depth of 1 turns launching off.
ENV_MAX_DEPTH = "MAX_AGENT_DEPTH"
DEFAULT_MAX_DEPTH = 3
ENV_MAX_LAUNCHES = "MAX_AGENT_LAUNCHES"
DEFAULT_MAX_LAUNCHES = 10
LAUNCH_TOOL_NAMES = ("launch_agent", "get_agent_schema")

# 4xx codes that can succeed on a retry; every other 4xx means the request itself is wrong.
RETRYABLE_4XX = {408, 409, 429}
MAX_ERROR_CHARS = 2000


class ModelSetupError(Exception):
    """The chat model could not be built (e.g. no API key). Retrying cannot help."""


def default_model_factory(agent: AgentBase) -> BaseChatModel:
    return ChatGoogleGenerativeAI(model=agent.model_name, thinking_level=agent.reasoning)


def is_non_retryable(exc: BaseException) -> bool:
    """Auth / config style errors, where another attempt would fail the same way."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ModelSetupError):
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


class AgentRunner:
    def __init__(
        self,
        db: Database,
        *,
        max_attempts: int = 3,
        retry_delay_s: float = 1.0,
        model_factory: ModelFactory = default_model_factory,
        store: RunStore | None = None,
        max_concurrent: int | None = None,
        max_depth: int | None = None,
        max_launches: int | None = None,
    ):
        """`store` is what the `store_get` / `store_set` tools read and write (default: a new RunStore).

        `max_concurrent` caps how many attempts (model calls) run at once across this runner. Default:
        `MAX_CONCURRENT_AGENT_RUNS` from the environment, else 10.

        `max_depth` is the most agents in one chain of launches, the first agent included (A launches B launches C is
        3). Default: `MAX_AGENT_DEPTH`, else 3. At 1 no agent can launch another. `max_launches` is the most launches
        one attempt of one agent may make. Default: `MAX_AGENT_LAUNCHES`, else 10.
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
        self.max_attempts = max_attempts
        self.retry_delay_s = retry_delay_s
        self._model_factory = model_factory

    async def run(
        self, agent_cls: type[AgentBase], agent_input: AgentInput, *, job_id: str | None = None
    ) -> AgentResult[Any]:
        """Run an agent. Runtime failures come back as a FAILED result; they are not raised.

        Programming errors still raise: an input that is not the agent's `input_model`, or a
        `job_id` that does not exist.
        """
        return await self._run(agent_cls, agent_input, job_id=job_id)

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
        the job and records the attempt that launched it."""
        agent = agent_cls()
        if not isinstance(agent_input, agent.input_model):
            raise TypeError(
                f"{agent.name} expects {agent.input_model.__name__}, got {type(agent_input).__name__}"
            )

        invocation_id = new_id()
        # Only an agent that has depth left to launch is told about its sub-agents, and given the tools.
        can_launch = bool(agent.subagents) and depth < self.max_depth
        system_prompt = agent.build_system_prompt(can_launch=can_launch)
        prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()
        tool_names = agent.tool_names + (list(LAUNCH_TOOL_NAMES) if can_launch else [])
        message = agent_input.model_dump_json(indent=2)
        result_cls = AgentResult[agent.output_model]
        started = time.perf_counter()

        logger.info(
            "invocation_started invocation_id=%s agent=%s version=%s model=%s reasoning=%s job_id=%s max_attempts=%d",
            invocation_id, agent.name, agent.version, agent.model_name, agent.reasoning, job_id,
            self.max_attempts,
        )

        agent_run_id = ""
        error = ""
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            agent_run_id = new_id()
            await self.db.create_run(
                agent_run_id=agent_run_id,
                invocation_id=invocation_id,
                attempt=attempt,
                job_id=job_id,
                agent_name=agent.name,
                agent_version=agent.version,
                model_name=agent.model_name,
                reasoning=agent.reasoning,
                tool_names=tool_names,
                system_prompt_hash=prompt_hash,
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
                        agent_run_id, (time.perf_counter() - wait_started) * 1000,
                    )
                await self.db.mark_run_running(agent_run_id)
                logger.info(
                    "attempt_started agent_run_id=%s invocation_id=%s attempt=%d/%d",
                    agent_run_id, invocation_id, attempt, self.max_attempts,
                )
                attempt_started = time.perf_counter()

                try:
                    context = RunContext(store=self.store, job_id=job_id, agent_run_id=agent_run_id)
                    output, input_tokens, output_tokens, reported_model = await self._attempt(
                        agent, agent_input, system_prompt, message, context, slot, depth, can_launch
                    )
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
                        agent_run_id, attempt, self.max_attempts, non_retryable, error,
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
                    latency_ms = (time.perf_counter() - started) * 1000
                    logger.info(
                        "invocation_finished invocation_id=%s status=SUCCESS attempts=%d input_tokens=%d output_tokens=%d latency_ms=%.0f",
                        invocation_id, attempt, input_tokens, output_tokens, latency_ms,
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
            invocation_id, attempt, latency_ms, error,
        )
        return result_cls(
            status=RunStatus.FAILED,
            error=error,
            agent_run_id=agent_run_id,
            attempts=attempt,
            latency_ms=latency_ms,
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
        name = agent_cls().name
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
                context.agent_run_id, agent_name, depth + 1, launches, self.max_launches, context.job_id,
            )
            slot.leave()  # the child needs a slot of its own
            try:
                result = await self._run(
                    type(child), child_input, job_id=context.job_id,
                    parent_agent_run_id=context.agent_run_id, depth=depth + 1,
                )
            finally:
                await slot.back()
            if result.status == RunStatus.SUCCESS:
                return json.dumps({"status": "SUCCESS", "agent": agent_name, "output": result.output.model_dump(mode="json")})
            return json.dumps({"status": "FAILED", "agent": agent_name, "error": result.error})

        return [launch_agent, get_agent_schema]

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
    ) -> tuple[AgentOutput, int, int, str | None]:
        try:
            model = self._model_factory(agent)
        except Exception as exc:
            raise ModelSetupError(str(exc)) from exc

        tools = list(agent.tools) + (self._launch_tools(agent, context, slot, depth) if can_launch else [])
        lc_agent = create_agent(
            model,
            tools=tools,  # only the allow-listed tools are ever bound
            system_prompt=system_prompt,
            response_format=agent.output_model,
            context_schema=RunContext,
        )
        result = await lc_agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"recursion_limit": agent.recursion_limit},
            context=context,  # reaches tools through ToolRuntime; the model never sees it
        )

        structured = result.get("structured_response")
        if structured is None:
            raise RuntimeError("The agent finished without a structured response")
        output = agent.output_model.model_validate(
            structured.model_dump() if hasattr(structured, "model_dump") else structured
        )
        agent.check_output(agent_input, output)  # may raise ValueError: a failed attempt, retried
        input_tokens, output_tokens, reported_model = _usage(result["messages"])
        return output, input_tokens, output_tokens, reported_model
