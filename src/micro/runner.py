"""AgentRunner: runs an agent definition through langgraph, validates the output, retries, and logs."""

import asyncio
import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from micro.context import RunContext
from micro.db import Database, new_id
from micro.models import AgentBase, AgentInput, AgentOutput, AgentResult, RunStatus
from micro.store import RunStore

logger = logging.getLogger(__name__)

ModelFactory = Callable[[AgentBase], BaseChatModel]

ENV_MAX_CONCURRENT = "MAX_CONCURRENT_AGENT_RUNS"
DEFAULT_MAX_CONCURRENT = 10

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


def max_concurrent_from_env() -> int:
    """The concurrency limit from `MAX_CONCURRENT_AGENT_RUNS`, or the default when unset."""
    raw = os.environ.get(ENV_MAX_CONCURRENT, "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENT
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{ENV_MAX_CONCURRENT} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{ENV_MAX_CONCURRENT} must be at least 1, got {value}")
    return value


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
    ):
        """`store` is what the `store_get` / `store_set` tools read and write (default: a new RunStore).

        `max_concurrent` caps how many attempts (model calls) run at once across this runner. Default:
        `MAX_CONCURRENT_AGENT_RUNS` from the environment, else 10.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_concurrent is None:
            max_concurrent = max_concurrent_from_env()
        elif max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
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
        agent = agent_cls()
        if not isinstance(agent_input, agent.input_model):
            raise TypeError(
                f"{agent.name} expects {agent.input_model.__name__}, got {type(agent_input).__name__}"
            )

        invocation_id = new_id()
        system_prompt = agent.build_system_prompt()
        prompt_hash = agent.system_prompt_hash
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
                tool_names=agent.tool_names,
                system_prompt_hash=prompt_hash,
                input=agent_input,
            )
            # The attempt row stays INIT while it waits for a slot, and is RUNNING only once it holds one.
            queued = self._slots.locked()
            if queued:
                logger.info("waiting_for_slot agent_run_id=%s limit=%d", agent_run_id, self.max_concurrent)
            wait_started = time.perf_counter()
            async with self._slots:
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
                        agent, agent_input, system_prompt, message, context
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

    async def _attempt(
        self, agent: AgentBase, agent_input: AgentInput, system_prompt: str, message: str, context: RunContext
    ) -> tuple[AgentOutput, int, int, str | None]:
        try:
            model = self._model_factory(agent)
        except Exception as exc:
            raise ModelSetupError(str(exc)) from exc

        lc_agent = create_agent(
            model,
            tools=agent.tools,  # only the allow-listed tools are ever bound
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
