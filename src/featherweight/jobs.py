"""A tiny job runner: run a pipeline as a job and keep the `jobs` row up to date. No framework."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from featherweight.db import Database
from featherweight.models import RunStatus

logger = logging.getLogger(__name__)

T = TypeVar("T")
MAX_ERROR_CHARS = 2000


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
) -> JobOutcome[T]:
    """Run `pipeline(job_id)` as a job: create it (or use `job_id`), mark it RUNNING, await the pipeline, then
    mark it SUCCESS, or FAILED with the error. A pipeline that raises is returned as a FAILED outcome, not raised.
    A cancelled run is marked FAILED ("Cancelled") and the cancellation is re-raised.

    `resume_from` is the id of an earlier job: the new job records it, and an agent run that the earlier job already
    finished, with the same agent and input, is answered from its row instead of calling the model. It cannot be
    combined with `job_id`: create the job with `db.create_job(name, resume_from=...)` and pass that.

    To run in the background and give the caller the id first: create the job yourself with `db.create_job`,
    then `asyncio.create_task(run_job(db, name, pipeline, job_id=job_id))`.
    """
    if job_id is not None and resume_from is not None:
        raise ValueError("Use either job_id or resume_from: create the job with db.create_job(name, resume_from=...)")
    job_id = job_id or await db.create_job(name, resume_from=resume_from)
    await db.set_job_status(job_id, RunStatus.RUNNING)
    logger.info("job_started job_id=%s name=%s", job_id, name)
    try:
        result = await pipeline(job_id)
    except asyncio.CancelledError:
        await db.set_job_status(job_id, RunStatus.FAILED, error="Cancelled")
        logger.warning("job_cancelled job_id=%s name=%s", job_id, name)
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        await db.set_job_status(job_id, RunStatus.FAILED, error=error)
        logger.error("job_failed job_id=%s name=%s error=%s", job_id, name, error, exc_info=exc)
        return JobOutcome[T](job_id=job_id, status=RunStatus.FAILED, error=error)
    await db.set_job_status(job_id, RunStatus.SUCCESS)
    logger.info("job_finished job_id=%s name=%s status=SUCCESS", job_id, name)
    return JobOutcome[T](job_id=job_id, status=RunStatus.SUCCESS, result=result)
