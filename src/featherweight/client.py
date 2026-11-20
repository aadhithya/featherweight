"""`JobClient`: submit a job, follow it, cancel it, and read its result, from any process.

The client and the job worker are independent: the queue and the database are all that connects them. The client writes the job
row and puts a message on the queue; it never runs a job. The result is read from the user's result store, because featherweight
keeps no results of its own. See docs/specs/submitted-jobs.md.
"""

import logging
from collections.abc import AsyncGenerator, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from featherweight.db import Database, JobEvent, JobRow
from featherweight.events import job_events, wait_for_job
from featherweight.fingerprint import error_fields, input_hash, text_hash
from featherweight.jobs import Job, as_job
from featherweight.models import AgentBase, RunStatus
from featherweight.observers import JobSubmitted, Observer, Observers
from featherweight.queue import DEAD_LETTERED, JobMessage, JobQueue
from featherweight.results import ResultKey, ResultStore, SqliteResultStore
from featherweight.sqlite import SqliteDatabase

logger = logging.getLogger(__name__)


class JobClient:
    """Submits jobs to a queue and follows them. `results` is the user's `ResultStore`, where the worker wrote the outputs; the
    default is the sqlite store of a `SqliteDatabase`, and with another database there is none, so pass one (or `None`, and
    `result` has nothing to read)."""

    def __init__(
        self,
        db: Database,
        queue: JobQueue,
        results: ResultStore | None | Literal["default"] = "default",
        observers: Sequence[Observer] = (),
    ) -> None:
        self.db = db
        self.queue = queue
        self._observers = Observers(observers)  # heard on `submit`: the carrier they return travels in the message
        self.results: ResultStore | None = (
            results
            if not isinstance(results, str)
            else (SqliteResultStore(db) if isinstance(db, SqliteDatabase) else None)
        )

    async def submit(
        self,
        job: type[Job] | type[AgentBase],
        input: BaseModel,
        *,
        resumable: bool = False,
        resume_from: str | None = None,
        job_id: str | None = None,
    ) -> str:
        """Create the job and put it on the queue; returns its id. `job` is a `Job` class, or a plain (non-streaming) agent
        class. The input must be an instance of the job's input model (`TypeError` otherwise).

        `resume_from` is a failed job to resume: a new job that points to it, whose finished steps are reused when it runs
        (the old job must have been `resumable`, or `ValueError` says nothing was kept). If the queue refuses the message, the
        job is marked `FAILED` and the error is raised."""
        declared = as_job(job)
        if not isinstance(input, declared.input_model):
            raise TypeError(f"{declared.name} expects a {declared.input_model.__name__}, got {type(input).__name__}")
        job_id = await self.db.create_job(
            declared.name,
            job_id,
            resume_from=resume_from,
            resumable=resumable,
            job_version=declared.version,
            job_hash=declared.job_hash(),
            input_hash=input_hash(input),
        )
        trace_context: dict[str, str] = {}
        if self._observers:  # nothing about a trace is stored: the carrier goes in the message and nowhere else
            trace_context = await self._observers.submitted(
                JobSubmitted(
                    job_id=job_id,
                    name=declared.name,
                    version=declared.version,
                    job_hash=declared.job_hash(),
                    input_hash=input_hash(input),
                    input=input.model_dump(mode="json") if self._observers.wants_content else None,
                )
            )
        try:
            await self.queue.enqueue(
                JobMessage(
                    job_id=job_id,
                    job_name=declared.name,
                    input=input.model_dump(mode="json"),
                    trace_context=trace_context,
                )
            )
        except Exception as exc:
            error_type, error_hash = error_fields(exc)
            await self.db.set_job_status(job_id, RunStatus.FAILED, error_type=error_type, error_hash=error_hash)
            logger.error("job_not_queued job_id=%s name=%s error_type=%s", job_id, declared.name, error_type)
            raise
        logger.info("job_submitted job_id=%s name=%s resume_from=%s", job_id, declared.name, resume_from)
        return job_id

    async def status(self, job_id: str) -> JobRow:
        """The job's row. `LookupError` for a job that does not exist."""
        job = await self.db.get_job(job_id)
        if job is None:
            raise LookupError(f"No such job: {job_id}")
        return job

    async def cancel(self, job_id: str) -> bool:
        """Cancel a job. A queued job becomes `CANCELLED` (its message stays on the queue: the worker sees the status when it
        arrives, and skips it). A running job gets `cancel_requested`, and its worker stops it. Returns False for a job that is
        already final, or does not exist."""
        if await self.db.cancel_queued_job(job_id):  # one atomic step: it still waited, so it will not run
            return True
        # It was taken by a worker, or is final, or does not exist: ask a running job to stop (False for the others).
        return await self.db.request_job_cancel(job_id)

    async def fail(self, job_id: str, error_type: str = DEAD_LETTERED) -> bool:
        """End a job that will never run: its message was dead-lettered by a queue that gave up on it. Call it from your dead-letter
        handler. The job becomes `FAILED` (a `failed` event) with this error type, in one atomic step, **only if it is not running**:
        it is still `INIT`, or `RUNNING` with an expired lease (its worker was lost). Returns `False` for a job that is running, is
        already final, or does not exist. The user resumes it, as any failed job."""
        return await self.db.fail_queued_job(job_id, error_type, text_hash(error_type))

    async def result(self, job: type[Job] | type[AgentBase], job_id: str) -> BaseModel | None:
        """The output of a job that succeeded, read from your result store and parsed into the job's output model; `None` while
        there is none (the job is not `SUCCESS`, the store has nothing, or `results` is `None`). `LookupError` for a job that
        does not exist. It never reads our tables, which hold no results."""
        row = await self.status(job_id)
        if (
            row.status is not RunStatus.SUCCESS
            or self.results is None
            or row.job_hash is None
            or row.input_hash is None
        ):
            return None
        stored: dict[str, Any] | None = await self.results.get(
            ResultKey(job_id=job_id, agent_hash=row.job_hash, input_hash=row.input_hash)
        )
        return None if stored is None else as_job(job).output_model.model_validate(stored)

    def events(self, job_id: str, after: int = 0) -> AsyncGenerator[JobEvent, None]:
        """The events of the job after `after`: the stored ones, then each as it happens, ending at a final one. Send it as SSE
        with `sse.encode`."""
        return job_events(self.db, job_id, after)

    async def wait(self, job_id: str, timeout: float | None = None) -> JobRow:
        """The job once it is final. `TimeoutError` after `timeout` seconds. A convenience for scripts and tests: a web handler
        should return the job id and let the caller follow `events`."""
        return await wait_for_job(self.db, job_id, timeout)
