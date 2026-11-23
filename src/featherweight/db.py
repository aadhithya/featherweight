"""The run log: what featherweight keeps of the orchestration, and the abstract class that a database implements.

The log is metadata only: jobs, and one row per attempt of an agent, with status, tokens, timing, the launch tree and hashes.
It never holds an input, an output, a message or the text of an error (see docs/specs/persistence.md). `Database` is the
abstract class; `SqliteDatabase` (local development) and `PostgresDatabase` (deployment) implement it. Records go in and rows
come out as Pydantic models, so the same constraints hold whatever the database is.
"""

import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from featherweight.models import RunStatus

HASH = r"^[0-9a-f]{64}$"  # a SHA-256, in hex
TERMINAL = (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED)


def new_id() -> str:
    return uuid.uuid4().hex


def default_db_path() -> Path:
    return Path(os.environ.get("FTW_DB_PATH", "featherweight.db"))


def now() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunStart(_Model):
    """What is known when an attempt is created. There is no input in it: only its hash."""

    agent_run_id: str
    invocation_id: str
    attempt: int = Field(ge=1)
    job_id: str | None = None
    parent_agent_run_id: str | None = None
    depth: int = Field(default=1, ge=1)
    agent_name: str
    agent_version: str
    agent_hash: str = Field(pattern=HASH)
    system_prompt_hash: str = Field(pattern=HASH)
    tools_hash: str = Field(pattern=HASH)
    provider: str
    model_name: str
    reasoning: str  # the effort or thinking level, as a short label
    settings: dict[str, Any]  # all the model settings
    input_hash: str = Field(pattern=HASH)
    identity_hash: str | None = Field(default=None, pattern=HASH)  # the hash of the run's identity; None: it had none
    reused_from_agent_run_id: str | None = None
    job_execution: int | None = (
        None  # the execution of the job that makes it; None: the job's `executions` when the run is created
    )
    thread_id: str | None = None  # a streaming run: the conversation the client named


class RunEnd(_Model):
    """What is known when an attempt ends. There is no output and no error text in it: only hashes and the error's type."""

    status: RunStatus
    output_hash: str | None = Field(default=None, pattern=HASH)
    error_type: str | None = None
    error_hash: str | None = Field(default=None, pattern=HASH)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    reported_model_name: str | None = None

    @model_validator(mode="after")
    def _terminal(self) -> Self:
        if self.status not in TERMINAL:
            raise ValueError(f"a run ends as SUCCESS, FAILED or CANCELLED, not {self.status.value}")
        if (self.error_type is None) != (self.error_hash is None):
            raise ValueError("error_type and error_hash go together")
        return self


class JobRow(_Model):
    job_id: str
    name: str
    status: RunStatus
    resumable: bool
    resumed_from: str | None
    parent_agent_run_id: str | None  # a background job: the attempt that started this job; None for any other job
    job_version: str | None  # the version of the job class that it runs; None for a job made without one
    job_hash: str | None  # the hash of the job's declaration; None for a job made without a job class
    input_hash: str | None  # a submitted job: the hash of its input
    cancel_requested: bool
    worker_id: str | None  # the worker that runs it (the last one, once it ended); None while it is queued
    lease_expires_at: (
        datetime | None
    )  # when the worker's lease on it ends; None for a job that has none (queued, final, or run in-process)
    executions: int  # how many times a worker started it: 1 for the first
    error_type: str | None
    error_hash: str | None
    created_at: datetime
    started_at: datetime | None  # when it began to run (cleared when it goes back to the queue)
    completed_at: datetime | None


class JobOwner(_Model):
    """Who owns a running job: the worker, and which execution of the job it is. The writes of a running job are conditional on
    both, in the statement itself, so that a worker that lost the job cannot change it."""

    worker_id: str
    executions: int = Field(ge=1)


class JobLost(Exception):
    """A write for a job that is not this worker's any more: another worker took it over, or it ended, or it was put back in the
    queue. Nothing was written. The worker that gets it stops the job and writes nothing more to it."""


EventType = Literal["queued", "running", "succeeded", "failed", "cancelled", "cancel_requested", "lost"]
# The event that a new status makes.
STATUS_EVENT: dict[RunStatus, EventType] = {
    RunStatus.INIT: "queued",
    RunStatus.RUNNING: "running",
    RunStatus.SUCCESS: "succeeded",
    RunStatus.FAILED: "failed",
    RunStatus.CANCELLED: "cancelled",
}
FINAL_EVENTS: tuple[EventType, ...] = ("succeeded", "failed", "cancelled")
FINAL = (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED)


class JobEvent(_Model):
    """A change in the life of a job. Metadata only: no content."""

    seq: int  # monotonic across all jobs
    job_id: str
    type: EventType
    status: RunStatus
    error_type: str | None = None
    error_hash: str | None = None
    at: datetime


class AgentRunRow(_Model):
    seq: int
    agent_run_id: str
    invocation_id: str
    attempt: int
    job_id: str | None
    parent_agent_run_id: str | None
    depth: int
    agent_name: str
    agent_version: str
    agent_hash: str
    system_prompt_hash: str
    tools_hash: str
    provider: str
    model_name: str
    reported_model_name: str | None
    reasoning: str
    settings: dict[str, Any]
    input_hash: str
    identity_hash: str | None = None  # the hash of the run's identity; None: it had none
    output_hash: str | None
    reused_from_agent_run_id: str | None
    job_execution: int | None  # the execution of the job that made the run (its `executions` then); None without a job
    thread_id: str | None
    status: RunStatus
    error_type: str | None
    error_hash: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class Database(ABC):
    """The run log. Subclass it to keep the log in a database of your own: write every abstract method, and run
    `featherweight.testing.check_database` against it. The schema is documented in docs/specs/persistence.md.

    Rows come back in the order they were made. Timestamps are timezone-aware `datetime`s: convert what your database stores.
    """

    def _publish(self, event: JobEvent) -> None:
        """Tell the listeners in this process about an event, once its transaction has committed. An implementation calls it
        after every event it writes; it needs nothing else for them. (Listeners in other processes read the table.)"""
        from featherweight.events import hub_for

        hub_for(self).publish(event)

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @abstractmethod
    async def connect(self) -> None:
        """Open the database and bring its schema up to date (the migrations)."""

    @abstractmethod
    async def close(self) -> None: ...

    # jobs

    async def create_job(
        self,
        name: str,
        job_id: str | None = None,
        *,
        resume_from: str | None = None,
        resumable: bool = False,
        job_version: str | None = None,
        job_hash: str | None = None,
        input_hash: str | None = None,
        parent_agent_run_id: str | None = None,
    ) -> str:
        """Create a job. `resumable=True` keeps the outputs of its steps (in the runner's result store), so that a later job
        can resume it; it is decided here, before the pipeline runs.

        With `resume_from`, the job resumes that one: its runs may reuse the successful steps of it. The old job must have
        been resumable, or nothing was kept and `ValueError` says so. A job that resumes another is resumable itself, so that
        a third attempt is possible.
        """
        if resume_from is not None:
            old = await self.get_job(resume_from)
            if old is None:
                raise LookupError(f"No such job to resume: {resume_from}")
            if not old.resumable:
                raise ValueError(
                    f"Job {resume_from} did not keep its results (it was not created with resumable=True), so there is "
                    "nothing to resume from. To run everything again, run the pipeline without resume_from."
                )
            resumable = True
        return await self.insert_job(
            job_id or new_id(), name, resume_from, resumable, job_version, job_hash, input_hash, parent_agent_run_id
        )

    @abstractmethod
    async def insert_job(
        self,
        job_id: str,
        name: str,
        resumed_from: str | None,
        resumable: bool,
        job_version: str | None,
        job_hash: str | None,
        input_hash: str | None,
        parent_agent_run_id: str | None = None,
    ) -> str:
        """Write a new job in status `INIT`, and return its id. The rules are checked in `create_job`."""

    @abstractmethod
    async def set_job_status(
        self,
        job_id: str,
        status: RunStatus,
        *,
        error_type: str | None = None,
        error_hash: str | None = None,
        owner: JobOwner | None = None,
    ) -> None:
        """Set the status of a job. The error is kept only for FAILED. `RUNNING` sets `started_at`; going back to `INIT` (a job
        that returns to the queue) clears it and its owner; a final status ends its lease. `LookupError` for a job that does not
        exist.

        With `owner`, the write is conditional, in the same statement, on the job being `RUNNING` and having that `worker_id`
        and `executions`: if it does not, nothing is written (no status, no event) and `JobLost` is raised."""

    @abstractmethod
    async def get_job(self, job_id: str) -> JobRow | None: ...

    @abstractmethod
    async def request_job_cancel(self, job_id: str) -> bool:
        """Ask for a job that is not final to be cancelled: set `cancel_requested` and write a `cancel_requested` event, in one
        transaction. Returns False, and changes nothing, for a job that does not exist or is already final. Asking twice writes
        one event. The job's worker does the cancelling."""

    @abstractmethod
    async def start_job(self, job_id: str, worker_id: str | None = None, lease: float | None = None) -> bool:
        """Mark a queued job `RUNNING` (`started_at`, a `running` event) **only if it is still `INIT` and has no
        `cancel_requested`**, in one atomic step, and say whether it did. It is what a worker calls to take a job: it and
        `cancel_queued_job` cannot both succeed, so a job is never cancelled and run at once.

        The same step records the owner: `worker_id`, `executions` one higher, and, if `lease` (seconds) is given, a
        `lease_expires_at` that far ahead (the database's own clock, where it has one). A job without a lease (one that
        runs in the process that started it) has no lease to lose."""

    @abstractmethod
    async def take_over_job(self, job_id: str, worker_id: str, lease: float) -> bool:
        """Take a job whose worker was lost: **only if it is `RUNNING` and its lease has expired**, in one atomic step, and say
        whether it did. The step writes a `lost` event (the job went back to `INIT`) and then starts the job for `worker_id`
        (a `running` event, `executions` one higher, a new lease), in one transaction. False means the lease is live (a duplicate
        delivery, or a slow worker that is still the owner), or the job is final, or another worker won. Where the database has
        a clock, it is the database's that decides when the lease expired."""

    @abstractmethod
    async def fail_queued_job(self, job_id: str, error_type: str, error_hash: str) -> bool:
        """Mark a job `FAILED` (a `failed` event with the error) **only if it is not running**: it is still `INIT`, or it is
        `RUNNING` with an expired lease (its worker was lost), in one atomic step, and say whether it did. It is for a job whose
        message will never be delivered again (dead-lettered): `JobClient.fail` calls it. False means the job is running, or is
        final, or does not exist."""

    @abstractmethod
    async def renew_job_leases(self, worker_id: str, jobs: Mapping[str, int], lease: float) -> set[str]:
        """A worker's heartbeat: in **one in-place update**, move `lease_expires_at` of each job in `jobs` (job id to the
        `executions` the worker holds) `lease` seconds ahead, for those that are still `RUNNING` with this `worker_id` and
        that `executions`. Returns the ids that were renewed: the others are lost. No rows are added, and nothing else changes."""

    @abstractmethod
    async def owns_job(self, job_id: str, owner: JobOwner) -> bool:
        """Whether the job is `RUNNING` and still has this owner. A cheap read, for a check before a step that cannot be made
        conditional (writing a result to the user's store); it is not atomic with what follows."""

    @abstractmethod
    async def cancel_queued_job(self, job_id: str) -> bool:
        """Mark a job `CANCELLED` (a `cancelled` event) **only if it is still `INIT`**, in one atomic step, and say whether it
        did. False means it was taken by a worker (or is final, or does not exist): the caller then asks for a running job to
        be cancelled with `request_job_cancel`."""

    @abstractmethod
    async def list_cancel_requested(self, job_ids: Sequence[str]) -> set[str]:
        """Which of these jobs have `cancel_requested` set and are not final yet, in one query. It is what a worker asks, about
        the jobs it is running, to learn that a client wants one of them cancelled."""

    # job events

    @abstractmethod
    async def list_job_events(self, job_id: str, after: int = 0, limit: int | None = None) -> list[JobEvent]:
        """The events of a job after the sequence number `after`, in order."""

    @abstractmethod
    async def poll_job_events(self, job_ids: Sequence[str], after: int = 0) -> list[JobEvent]:
        """The events of any of these jobs after `after`, in order, in one query. It is what the one reader of a process asks
        for, whatever the number of streams that are open."""

    # agent runs

    @abstractmethod
    async def create_run(self, run: RunStart) -> str:
        """Write an attempt in status `INIT` (waiting for a slot), and return its id. A `job_id` that does not exist is an
        error."""

    @abstractmethod
    async def mark_run_running(self, agent_run_id: str) -> None:
        """The attempt holds a slot: status `RUNNING`, and `started_at` is now."""

    @abstractmethod
    async def finish_run(self, agent_run_id: str, end: RunEnd) -> None:
        """End the attempt. `LookupError` for a run that does not exist."""

    @abstractmethod
    async def get_run(self, agent_run_id: str) -> AgentRunRow | None: ...

    @abstractmethod
    async def find_reusable_run(
        self,
        job_id: str,
        agent_hash: str,
        input_hash: str,
        identity_hash: str | None,
        before_execution: int | None = None,
    ) -> AgentRunRow | None:
        """The first top-level run (depth 1) of `job_id` that succeeded with this agent, this input and this identity, or None. The
        identity matches by its hash, and `None` (no identity) matches only a run that had none. With
        `before_execution`, only a run of an earlier execution of the job (`job_execution` below it): that is how a takeover
        looks in its own job without finding the runs of the execution that asks."""

    @abstractmethod
    async def list_runs(self, *, job_id: str | None = None, invocation_id: str | None = None) -> list[AgentRunRow]:
        """Runs in the order they were made, optionally of one job and/or one invocation."""
