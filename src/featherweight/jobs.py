"""Jobs: `run_job` runs a pipeline you hold as a job, and a `Job` is a declared class that a worker can run by name.

A `Job` has a name, a version, an input model and an output model (Pydantic), and `async run(ctx, input)`. A plain agent can
be a job too (`as_job`). A job is what a client submits to a queue and a job worker runs (see docs/specs/submitted-jobs.md).
"""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from featherweight.db import Database
from featherweight.fingerprint import agent_hash, error_fields, job_hash
from featherweight.models import AgentBase, AgentInput, AgentResult, RunStatus
from featherweight.runner import AgentRunner

logger = logging.getLogger(__name__)

T = TypeVar("T")
MAX_ERROR_CHARS = 2000
JOB_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


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

    The checks are made when the class is defined: a missing or odd name or version, or a model that is not Pydantic, is an
    error then, not when a job arrives. A class that has not written `run` cannot be built."""

    name: ClassVar[str]
    version: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

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

    @classmethod
    def job_hash(cls) -> str:
        """SHA-256 of the declaration: it changes when the name, the version or either schema does."""
        return job_hash(cls.name, cls.version, cls.input_model, cls.output_model)

    @abstractmethod
    async def run(self, ctx: JobContext, input: Any) -> Any:
        """Do the job: run agents with `ctx.run`, and return an instance of `output_model`."""


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
    status: RunStatus  # SUCCESS or FAILED
    result: T | None = None  # what the pipeline returned, on SUCCESS
    error: str | None = None  # `ExceptionType: message`, on FAILED


async def run_job(
    db: Database,
    name: str,
    pipeline: Callable[[str], Awaitable[T]],
    *,
    job_id: str | None = None,
    resume_from: str | None = None,
    resumable: bool = False,
) -> JobOutcome[T]:
    """Run `pipeline(job_id)` as a job: create it (or use `job_id`), mark it RUNNING, await the pipeline, then
    mark it SUCCESS, or FAILED with the error. A pipeline that raises is returned as a FAILED outcome, not raised.
    A cancelled run is marked CANCELLED and the cancellation is re-raised.

    `resumable=True` keeps the outputs of the job's steps, so that a later job can resume it. It is decided here, before the
    pipeline runs: a job that did not keep them cannot be resumed.

    `resume_from` is the id of an earlier, resumable job: the new job records it, and a step that the earlier job already
    finished, with the same agent and input, is answered from the kept result instead of calling the model. `resume_from`
    and `resumable` cannot be combined with `job_id`: create the job with `db.create_job(name, resume_from=...)` and pass
    that.

    To run in the background and give the caller the id first: create the job yourself with `db.create_job`,
    then `asyncio.create_task(run_job(db, name, pipeline, job_id=job_id))`.
    """
    if job_id is not None and (resume_from is not None or resumable):
        raise ValueError(
            "Use either job_id or resume_from/resumable: create the job with db.create_job(name, resume_from=..., "
            "resumable=...)"
        )
    job_id = job_id or await db.create_job(name, resume_from=resume_from, resumable=resumable)
    await db.set_job_status(job_id, RunStatus.RUNNING)
    logger.info("job_started job_id=%s name=%s", job_id, name)
    try:
        result = await pipeline(job_id)
    except asyncio.CancelledError:
        await db.set_job_status(job_id, RunStatus.CANCELLED)
        logger.warning("job_cancelled job_id=%s name=%s", job_id, name)
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        error_type, error_hash = error_fields(exc)  # the text is returned and logged, not kept
        await db.set_job_status(job_id, RunStatus.FAILED, error_type=error_type, error_hash=error_hash)
        logger.error("job_failed job_id=%s name=%s error=%s", job_id, name, error, exc_info=exc)
        return JobOutcome[T](job_id=job_id, status=RunStatus.FAILED, error=error)
    await db.set_job_status(job_id, RunStatus.SUCCESS)
    logger.info("job_finished job_id=%s name=%s status=SUCCESS", job_id, name)
    return JobOutcome[T](job_id=job_id, status=RunStatus.SUCCESS, result=result)
