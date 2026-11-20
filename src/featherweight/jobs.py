"""Jobs: a `Job` is a declared class, run in your process by `run_job` or on a worker by name, by the same code (`execute_job`).

A `Job` has a name, a version, an input model and an output model (Pydantic), and `async run(ctx, input)`. A plain agent can
be a job too (`as_job`). A job is what a client submits to a queue and a job worker runs (see docs/specs/submitted-jobs.md).
"""

import asyncio
import inspect
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, Generic, TypeVar, get_type_hints, overload

from pydantic import BaseModel, ConfigDict

from featherweight.context import CURRENT_JOB_EXECUTION, IN_BACKGROUND_JOB
from featherweight.db import JobLost, JobOwner, new_id
from featherweight.fingerprint import agent_hash, error_fields, input_hash, job_hash
from featherweight.models import AgentBase, AgentInput, AgentResult, RunStatus
from featherweight.observers import Entered, JobEnd, JobStart
from featherweight.results import ResultKey, ResultStore
from featherweight.runner import AgentRunner

logger = logging.getLogger(__name__)

T = TypeVar("T")
MAX_ERROR_CHARS = 2000
JOB_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")

DEFAULT_JOB_TIMEOUT = 600.0  # seconds: ten minutes


class JobTimeout(TimeoutError):
    """A job ran for longer than its `timeout`. It is the error type that the job fails with."""


def _check_timeout(label: str, timeout: Any) -> None:
    if timeout is None:
        return
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not timeout > 0:
        raise TypeError(f"{label}.timeout must be a positive number of seconds, or None for no limit")


class JobContext(BaseModel):
    """What a job gets when it runs: the runner and its own id. `run` and `run_many` run agents **in this job**, so that they
    share its trace and its resume."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    runner: AgentRunner
    job_id: str

    async def run(self, agent_cls: type[AgentBase], agent_input: AgentInput) -> AgentResult[Any]:
        return await self.runner.run(agent_cls, agent_input, job_id=self.job_id)

    async def run_many(self, agent_cls: type[AgentBase], agent_inputs: Sequence[AgentInput]) -> list[AgentResult[Any]]:
        return await self.runner.run_many(agent_cls, agent_inputs, job_id=self.job_id)


class Job(ABC):
    """A job, declared as a class, like an agent. Subclass it, set `name`, `version`, `input_model` and `output_model`, and write
    `run`. A job worker is given the classes it can run, and runs one by its name.

    The checks are made when the class is defined: a missing or odd name or version, or a model that is not Pydantic, or a
    `timeout` that is not a positive number or `None`, is an error then, not when a job arrives. A class that has not written
    `run` cannot be built.

    `timeout` is how many seconds one execution of the job may run (default 600; `None` for no limit). A job that exceeds it is
    cancelled and fails with error type `JobTimeout`, in a worker and in `run_job` alike. It is not part of `job_hash`."""

    name: ClassVar[str]
    version: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    timeout: ClassVar[float | None] = DEFAULT_JOB_TIMEOUT  # seconds one execution may run; None for no limit

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls.run, "__isabstractmethod__", False):  # a base for other jobs, not a job
            return
        name, version = getattr(cls, "name", None), getattr(cls, "version", None)
        if not isinstance(name, str) or not JOB_NAME.match(name):
            raise TypeError(
                f"{cls.__name__}.name must be a string of letters, digits, '_', '.' or '-', starting with a letter"
            )
        if not isinstance(version, str) or not version:
            raise TypeError(f"{cls.__name__}.version must be a non-empty string")
        for attribute in ("input_model", "output_model"):
            model = getattr(cls, attribute, None)
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise TypeError(f"{cls.__name__}.{attribute} must be a Pydantic model class")
        _check_timeout(cls.__name__, cls.timeout)

    @classmethod
    def job_hash(cls) -> str:
        """SHA-256 of the declaration: it changes when the name, the version or either schema does."""
        return job_hash(cls.name, cls.version, cls.input_model, cls.output_model)

    @abstractmethod
    async def run(self, ctx: JobContext, input: Any) -> Any:
        """Do the job: run agents with `ctx.run`, and return an instance of `output_model`."""


def _build_job(
    fn: Callable[..., Awaitable[Any]], name: str | None, version: str, timeout: float | None = DEFAULT_JOB_TIMEOUT
) -> type[Job]:
    """The `Job` class of a function `async def f(ctx: JobContext, input: In) -> Out`."""
    label = getattr(fn, "__name__", repr(fn))
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"@job needs an async function, and {label} is not one")
    parameters = list(inspect.signature(fn).parameters.values())
    if len(parameters) != 2:
        raise TypeError(f"@job needs a function of two arguments (ctx, input), and {label} has {len(parameters)}")
    hints = get_type_hints(fn)
    input_model, output_model = hints.get(parameters[1].name), hints.get("return")
    for what, model in (("the input", input_model), ("the return value", output_model)):
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(
                f"@job reads the models from the annotations: {what} of {label} must be annotated with a Pydantic model"
            )

    async def run(self: Job, ctx: JobContext, input: Any) -> Any:
        return await fn(ctx, input)

    run.__doc__ = fn.__doc__
    return type(
        label,
        (Job,),
        {
            "name": name or label,
            "version": version,
            "input_model": input_model,
            "output_model": output_model,
            "timeout": timeout,
            "run": run,
            "function": staticmethod(fn),
            "__doc__": fn.__doc__,
            "__module__": getattr(fn, "__module__", __name__),
        },
    )


@overload
def job(fn: Callable[..., Awaitable[Any]], /) -> type[Job]: ...
@overload
def job(
    *, name: str | None = None, version: str = "1", timeout: float | None = DEFAULT_JOB_TIMEOUT
) -> Callable[[Callable[..., Awaitable[Any]]], type[Job]]: ...
def job(
    fn: Callable[..., Awaitable[Any]] | None = None,
    /,
    *,
    name: str | None = None,
    version: str = "1",
    timeout: float | None = DEFAULT_JOB_TIMEOUT,
) -> Any:
    """Make a `Job` class from an async function: `async def summarise(ctx: JobContext, input: In) -> Out`. The name is the
    function's (or `name=`), the version `"1"` (or `version=`), and the input and output models are the annotations of `input` and
    the return value. `timeout=` is the seconds one execution may run (default 600, `None` for no limit). The function becomes the class, usable wherever a `Job` is; the function itself is `Summarise.function`.

        @job
        async def summarise(ctx: JobContext, input: SummariseInput) -> SummariseOutput: ...

        @job(name="summarise", version="2", timeout=120)
        async def summarise(ctx, input: SummariseInput) -> SummariseOutput: ...
    """
    if fn is not None:
        return _build_job(fn, name, version, timeout)
    return lambda function: _build_job(function, name, version, timeout)


class AgentRunFailed(RuntimeError):
    """An agent that was run as a job failed (its attempts were used up). The message is the agent's error."""


_AGENT_JOBS: dict[type[AgentBase], type[Job]] = {}


def as_job(job: type[Job] | type[AgentBase]) -> type[Job]:
    """A `Job` class as it is, or a plain agent class as a job: its name, its input model, its output model, and `agent_hash`
    as the job hash. A streaming agent cannot be a job (it streams to a person)."""
    if isinstance(job, type) and issubclass(job, Job):
        return job
    if not (isinstance(job, type) and issubclass(job, AgentBase)):
        raise TypeError(f"{job!r} is neither a Job nor an agent class")
    if job.streaming:
        raise TypeError(f"{job.__name__} is a streaming agent: it streams to a person and cannot be a job")
    if job not in _AGENT_JOBS:
        agent = job()  # ty: ignore[missing-argument]  # a subclass supplies the defaults

        class AgentJob(Job):
            name = agent.name
            version = agent.version
            input_model = job.input_model
            output_model = job.output_model

            @classmethod
            def job_hash(cls) -> str:
                return agent_hash(job())  # ty: ignore[missing-argument]

            async def run(self, ctx: JobContext, input: Any) -> Any:
                result = await ctx.run(job, input)
                if result.status is not RunStatus.SUCCESS or result.output is None:
                    raise AgentRunFailed(result.error or "the agent failed")
                return result.output

        AgentJob.__name__ = AgentJob.__qualname__ = f"{job.__name__}Job"
        _AGENT_JOBS[job] = AgentJob
    return _AGENT_JOBS[job]


class JobOutcome(BaseModel, Generic[T]):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    job_id: str
    status: RunStatus  # SUCCESS or FAILED (CANCELLED or INIT when the job was not started: see `run_job`)
    result: T | None = None  # the job's output model, on SUCCESS
    error: str | None = None  # `ExceptionType: message`, on FAILED


class InvalidJobOutput(Exception):
    """A job returned something that is not an instance of its output model."""


async def execute_job(
    runner: AgentRunner,
    job: type[Job],
    job_id: str,
    job_input: BaseModel,
    results: ResultStore | None,
    *,
    on_cancel: Callable[[], Awaitable[None]] | None = None,
    trace_context: Mapping[str, str] = {},
    owner: JobOwner | None = None,
    lost: Callable[[], bool] | None = None,
) -> JobOutcome[BaseModel]:
    """Run a job that has been started (its row is `RUNNING`), keep its result and set its final status. This is the one function
    that runs a job, for `run_job` (in your process) and for the job worker, so that the two cannot differ: the same row, hashes,
    events, result, and the same calls to the runner's observers.

    A job that raises is a `FAILED` outcome, not an exception (the error type and hash go to the run log, the text to the log and
    the outcome). A cancelled task sets `CANCELLED` (or runs `on_cancel`, which the worker uses to put a job back on the queue)
    and re-raises the cancellation.

    The observers' `job` scope wraps the whole run, so that an observer can make its span the current one while the pipeline
    runs. `trace_context` is the carrier that came with the message (a worker), and is empty for `run_job`.

    `owner` (who started the job) makes every write to the job conditional on it still being the owner, in the statement itself.
    A write that finds the job taken from it raises `JobLost` and writes nothing: this function lets it through (the observers'
    scope ends with error type `JobLost`, for tracing only) and the caller drops the job. A task that is cancelled because the
    job was lost (`lost()` says so) writes nothing to the job either."""
    observers = runner._observers
    scope: Entered | None = None
    row = await runner.db.get_job(job_id)
    in_background = row is not None and row.parent_agent_run_id is not None  # started as a background agent
    if observers:
        scope = await observers.enter(
            "job",
            JobStart(
                job_id=job_id,
                name=job.name,
                version=job.version,
                job_hash=(row.job_hash if row else None) or job.job_hash(),
                input_hash=(row.input_hash if row else None) or input_hash(job_input),
                resumable=row.resumable if row else False,
                resumed_from=row.resumed_from if row else None,
                execution=owner.executions if owner is not None else None,
                trace_context=dict(trace_context),
                input=job_input.model_dump(mode="json") if observers.wants_content else None,
            ),
        )
    try:
        token = IN_BACKGROUND_JOB.set(True) if in_background else None  # what it runs cannot start background agents
        try:
            outcome, error_type = await _execute(runner, job, job_id, job_input, results, on_cancel, owner)
        finally:
            if token is not None:
                IN_BACKGROUND_JOB.reset(token)
    except asyncio.CancelledError:
        if scope is not None:
            if lost is not None and lost():  # for tracing only: nothing is written to the job
                await scope.leave(JobEnd(status=RunStatus.FAILED, error_type="JobLost"))
            else:
                await scope.leave(JobEnd(status=RunStatus.CANCELLED))
        raise
    except JobLost:
        if scope is not None:
            await scope.leave(JobEnd(status=RunStatus.FAILED, error_type="JobLost"))
        raise
    if scope is not None:
        await scope.leave(
            JobEnd(
                status=outcome.status,
                error_type=error_type,
                output=outcome.result.model_dump(mode="json") if observers.wants_content and outcome.result else None,
                error=outcome.error if observers.wants_content else None,
            )
        )
    return outcome


async def _run_limited(job: type[Job], ctx: JobContext, job_input: BaseModel) -> Any:
    """`job.run`, for at most `job.timeout` seconds. Past that the work is cancelled (an agent run in it ends `CANCELLED`, as for a
    cancel) and `JobTimeout` is raised, which makes the job `FAILED`. Only the limit's own expiry is a timeout: a `TimeoutError`
    that the job raises itself, and a cancellation from outside (the user's, a shutdown), are not."""
    try:
        async with asyncio.timeout(job.timeout) as limit:
            return await job().run(ctx, job_input)
    except TimeoutError as exc:
        if limit.expired():
            raise JobTimeout(f"{job.name} ran for more than {job.timeout:g} seconds") from exc
        raise


async def _execute(
    runner: AgentRunner,
    job: type[Job],
    job_id: str,
    job_input: BaseModel,
    results: ResultStore | None,
    on_cancel: Callable[[], Awaitable[None]] | None,
    owner: JobOwner | None,
) -> tuple[JobOutcome[BaseModel], str | None]:
    """The run itself, and the type of the error if it failed."""
    logger.info("job_started job_id=%s job=%s", job_id, job.name)
    try:
        token = CURRENT_JOB_EXECUTION.set((job_id, owner.executions)) if owner is not None else None
        try:
            output = await _run_limited(job, JobContext(runner=runner, job_id=job_id), job_input)
        finally:
            if token is not None:
                CURRENT_JOB_EXECUTION.reset(token)
        if not isinstance(output, job.output_model):
            raise InvalidJobOutput(f"{job.name} returned {type(output).__name__}, not {job.output_model.__name__}")
        if results is not None:  # the output is the job's product: if it cannot be kept, the job fails
            if owner is not None and not await runner.db.owns_job(job_id, owner):
                # Not atomic with the write below (the store is the user's), so a very small window remains: for the same input
                # the later write wins. Better than writing a result for a job that another worker has taken.
                raise JobLost(f"Job {job_id} is not owned by {owner} any more")
            row = await runner.db.get_job(job_id)
            if row is not None and row.job_hash and row.job_hash != job.job_hash():
                logger.warning(
                    "job_hash_differs job_id=%s: this process's %s is not the one that was submitted", job_id, job.name
                )
            key = ResultKey(
                job_id=job_id,
                agent_hash=(row.job_hash if row else None) or job.job_hash(),
                input_hash=(row.input_hash if row else None) or input_hash(job_input),
            )
            await results.put(key, output.model_dump(mode="json"))
    except asyncio.CancelledError:
        if on_cancel is not None:
            await on_cancel()
        else:
            await runner.db.set_job_status(job_id, RunStatus.CANCELLED, owner=owner)
            logger.warning("job_cancelled job_id=%s job=%s", job_id, job.name)
        raise
    except JobLost:
        raise  # not an error of the job: it is not ours any more, and nothing is written to it
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        error_type, error_hash = error_fields(exc)  # the text is returned and logged, not kept
        await runner.db.set_job_status(
            job_id, RunStatus.FAILED, error_type=error_type, error_hash=error_hash, owner=owner
        )
        logger.error("job_failed job_id=%s job=%s error=%s", job_id, job.name, error, exc_info=exc)
        return JobOutcome[BaseModel](job_id=job_id, status=RunStatus.FAILED, error=error), error_type
    await runner.db.set_job_status(job_id, RunStatus.SUCCESS, owner=owner)
    logger.info("job_finished job_id=%s job=%s status=SUCCESS", job_id, job.name)
    return JobOutcome[BaseModel](job_id=job_id, status=RunStatus.SUCCESS, result=output), None


async def run_job(
    runner: AgentRunner,
    job: type[Job] | type[AgentBase],
    input: BaseModel,
    *,
    job_id: str | None = None,
    resume_from: str | None = None,
    resumable: bool = False,
) -> JobOutcome[Any]:
    """Run a job in your process: create it (or use `job_id`), start it, run it with `execute_job` (the code a worker runs it
    with), and return the outcome. `job` is a `Job` class, or a plain (non-streaming) agent class; `input` must be an instance
    of its input model (`TypeError` otherwise). The output is the job's output model, in `outcome.result`, and it is also written
    to the runner's result store if the runner has one. A job that raises is a FAILED outcome, not an exception; a cancelled
    task is marked CANCELLED and the cancellation is re-raised.

    `resumable=True` keeps the outputs of the job's steps, so that a later job can resume it. It is decided here, before the job
    runs: a job that did not keep them cannot be resumed. `resume_from` is the id of an earlier, resumable job: the new job
    records it, and a step that the earlier job already finished, with the same agent and input, is answered from the kept result
    instead of calling the model. `resume_from` and `resumable` cannot be combined with `job_id`: create the job with
    `db.create_job(name, resume_from=...)` and pass that.

    To run in the background and give the caller the id first: create the job yourself with `db.create_job`, then
    `asyncio.create_task(run_job(runner, job, input, job_id=job_id))`. A job that was cancelled (or started) before it could
    start is not run: the outcome has the status it has and no result.
    """
    declared = as_job(job)
    if not isinstance(input, declared.input_model):
        raise TypeError(f"{declared.name} expects a {declared.input_model.__name__}, got {type(input).__name__}")
    if job_id is not None and (resume_from is not None or resumable):
        raise ValueError(
            "Use either job_id or resume_from/resumable: create the job with db.create_job(name, resume_from=..., "
            "resumable=...)"
        )
    db = runner.db
    job_id = job_id or await db.create_job(
        declared.name,
        resume_from=resume_from,
        resumable=resumable,
        job_version=declared.version,
        job_hash=declared.job_hash(),
        input_hash=input_hash(input),
    )
    # The same compare-and-set step as the worker's. A job run in your process has an owner (this call) and no lease: nothing
    # renews one, and nobody else takes it over, so there is no lease to lose.
    worker_id = f"process-{new_id()}"
    if not await db.start_job(job_id, worker_id=worker_id):
        row = await db.get_job(job_id)
        status = row.status if row is not None else RunStatus.FAILED
        logger.info("job_not_started job_id=%s status=%s", job_id, status.value)
        return JobOutcome[Any](job_id=job_id, status=status, error=f"The job is {status.value}, so it did not run")
    started = await db.get_job(job_id)
    owner = JobOwner(worker_id=worker_id, executions=started.executions) if started is not None else None
    return await execute_job(runner, declared, job_id, input, runner.results, owner=owner)
