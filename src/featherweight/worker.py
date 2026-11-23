"""The job worker: the process that runs submitted jobs.

It receives messages from a queue, runs the job each one names, writes the output through the user's result store, and records
the outcome in the run log. It holds no state of its own beyond the tasks it is running: everything else is the database, the
queue and the user's store. A failed job is never retried here (the user resumes it). See docs/specs/submitted-jobs.md.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from featherweight.db import FINAL, Database, JobLost, JobOwner, JobRow, new_id
from featherweight.fingerprint import error_fields
from featherweight.identity import IdentitySpec
from featherweight.jobs import Job, as_job, execute_job
from featherweight.models import AgentBase, RunStatus
from featherweight.queue import Delivery, JobQueue
from featherweight.results import ResultStore, SqliteResultStore
from featherweight.runner import AgentRunner
from featherweight.sqlite import SqliteDatabase

logger = logging.getLogger(__name__)


class UnknownJob(Exception):
    """A message named a job that this worker was not given."""


class InvalidJobInput(Exception):
    """The input of a message does not fit the input model of its job."""


class JobLookupError(Exception):
    """`jobs_lookup` raised, returned something that is not a job, returned a job with another name than the one asked for, or returned a
    job whose hash is not the one recorded when the job was submitted (it changed while the job waited). The job is not run."""


class InvalidJobIdentity(Exception):
    """The identity of a message is missing and the worker's `identity_type` requires one, or does not fit that type, or the worker
    declares no type. The job is not run."""


# `async def jobs_lookup(name, job_hash, identity) -> Job class | agent class | None`: see `JobWorker`.
JobsLookup = Callable[[str, "str | None", Any], Awaitable["type[Job] | type[AgentBase] | None"]]


class JobWorker:
    """`JobWorker(db, queue, runner, jobs, results)`: `jobs` are the `Job` classes (or plain agent classes) it can run, by name.

    `results` is where the output of a job is written (a `ResultStore`; the default is a sqlite store in the run log's file
    for a `SqliteDatabase`, and none for another database; `None` keeps nothing). `concurrency` is how many jobs run at once.

    `lease` is how long, in seconds, the worker's claim on a job lasts without being renewed (60 by default). While it runs
    jobs, the worker renews the lease of each, and `extend`s the queue's lease of its message, every `heartbeat` seconds (a third of
    the lease by default). A worker that lost a job (another one took it over) stops it and writes nothing more to it.
    `worker_id` names the worker in `jobs.worker_id` (a new id by default).

    `identity_type` is the type of the identity of a run (docs/specs/identity.md): the worker rebuilds the identity of a message with it and
    gives it to the run. A type that does not allow `None` makes a message without an identity fail its job (`InvalidJobIdentity`), and so
    does an identity that does not fit the type, or one when no type is declared. The job is not run, and not retried.

    `jobs_lookup` is a fallback for jobs that are not declared in code (an agent that a platform's user defined, kept in the platform's own
    storage): `async def jobs_lookup(name, job_hash, identity)` returns the job class (or a plain agent class) for the name, or `None`.
    `jobs` are tried first; the lookup is called only for a name `jobs` does not have, with the `job_hash` recorded when the job was
    submitted (None for a job that has none) and the rebuilt identity (None if there is none). The worker checks that what it returns has the
    name that was asked for and the recorded hash, and caches nothing. `None` is `UnknownJob`; a lookup that raises, a result that is
    not a job, another name and another hash fail the job with `JobLookupError`. `jobs` may be empty. See docs/specs/submitted-jobs.md.

        worker = JobWorker(db, queue, runner, jobs=[Summarise])
        await worker.run()           # until `await worker.stop()` is called, from another task
    """

    def __init__(
        self,
        db: Database,
        queue: JobQueue,
        runner: AgentRunner,
        jobs: Sequence[type[Job] | type[AgentBase]],
        results: ResultStore | None | Literal["default"] = "default",
        *,
        concurrency: int = 10,
        receive_timeout: float = 1.0,
        cancel_poll: float = 1.0,
        lease: float = 60.0,
        heartbeat: float | None = None,
        worker_id: str | None = None,
        identity_type: Any = None,
        jobs_lookup: JobsLookup | None = None,
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if lease <= 0 or (heartbeat is not None and heartbeat <= 0):
            raise ValueError("lease and heartbeat must be positive")
        self.db = db
        self.queue = queue
        self.runner = runner
        self.results: ResultStore | None = (
            results
            if not isinstance(results, str)
            else (SqliteResultStore(db) if isinstance(db, SqliteDatabase) else None)
        )
        self.concurrency = concurrency
        self.receive_timeout = receive_timeout
        self.cancel_poll = cancel_poll
        self.lease = lease
        self.heartbeat = heartbeat if heartbeat is not None else lease / 3
        self.worker_id = worker_id or f"worker-{new_id()}"
        self._identity = IdentitySpec(identity_type)  # what a message's identity is rebuilt as (docs/specs/identity.md)
        self._jobs_lookup = jobs_lookup
        self._jobs: dict[str, type[Job]] = {}
        for job in jobs:
            declared = as_job(job)
            if declared.name in self._jobs and self._jobs[declared.name] is not declared:
                raise ValueError(f"Two jobs are called {declared.name!r}: a worker runs a job by its name")
            self._jobs[declared.name] = declared
        self._tasks: dict[str, asyncio.Task[None]] = {}  # the jobs running now, by job id
        self._deliveries: dict[str, Delivery] = {}
        self._owners: dict[str, JobOwner] = {}  # who owns each job running here: this worker, and the execution
        self._lost: set[str] = set()  # the jobs that were found taken from this worker
        self._requeue: set[str] = set()  # the jobs that are cancelled by a shutdown, to go back to the queue
        self._wake = asyncio.Event()  # a job ended, or the worker was asked to stop
        self._stopping = False
        self._grace = 30.0
        self._running = False
        self._stopped = asyncio.Event()

    # running

    async def run(self) -> None:
        """Receive and run jobs until `stop()` is called. If the task that runs this is cancelled, the running jobs are
        cancelled and returned to the queue, and the cancellation is raised."""
        if self._running:
            raise RuntimeError("This worker is already running")
        self._running, self._stopping = True, False
        self._stopped.clear()
        poller = asyncio.create_task(self._poll_cancels())
        heart = asyncio.create_task(self._heartbeat())
        try:
            await self._loop()
            await self._shutdown(self._grace)
        except asyncio.CancelledError:
            await self._shutdown(0)
            raise
        finally:
            poller.cancel()
            heart.cancel()
            self._running = False
            self._stopped.set()

    async def stop(self, grace: float = 30.0) -> None:
        """Take no new messages, wait up to `grace` seconds for the running jobs, then cancel the ones still running and
        return their messages to the queue (`nack` with `requeue=True`): those jobs go back to `INIT`, and run again."""
        if not self._running:
            return
        self._grace = grace
        self._stopping = True
        self._wake.set()
        await self._stopped.wait()

    def cancel(self, job_id: str) -> bool:
        """Cancel a job this worker is running, at once (in this process). False if it is not running here. A client in
        another process sets `cancel_requested` instead, and the worker finds it within `cancel_poll` seconds."""
        task = self._tasks.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def _loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            if len(self._tasks) >= self.concurrency:  # full: wait for a job to end
                await self._wake.wait()
                continue
            delivery = await self.queue.receive(self.receive_timeout)
            if delivery is None:
                continue
            if self._stopping:  # arrived as it stopped: not ours to run
                await self.queue.nack(delivery, requeue=True)
                break
            try:
                await self._start(delivery)
            except Exception:
                logger.exception("worker_message_failed job_id=%s", delivery.message.job_id)
                await self.queue.nack(delivery, requeue=False)

    async def _start(self, delivery: Delivery) -> None:
        message = delivery.message
        row = await self.db.get_job(message.job_id)
        if row is None:
            logger.warning("worker_unknown_job_row job_id=%s: the message is dropped", message.job_id)
            await self.queue.ack(delivery)
            return
        if row.status in FINAL:  # cancelled while it waited, or a redelivery of a job that ended
            logger.info("worker_skipped job_id=%s status=%s", row.job_id, row.status.value)
            await self.queue.ack(delivery)
            return
        if (
            row.job_id in self._tasks
        ):  # this worker is running it: the message came back while it does. Its heartbeat keeps the job
            logger.info(
                "worker_duplicate job_id=%s: this worker is running it", row.job_id
            )  # (renewing needs only the ownership)
            await self.queue.ack(delivery)
            return
        # A message for a job that is RUNNING came back after its lease ended. If the job's lease has not expired, that is a duplicate
        # delivery (or a slow owner): leave the job alone. If it has, its worker was lost: take it over, as a new execution.
        # The database decides, in one step, by its own clock.
        taken_over = False
        if row.status is RunStatus.RUNNING:
            if not await self.db.take_over_job(row.job_id, self.worker_id, self.lease):
                logger.info("worker_duplicate job_id=%s: another worker has it", row.job_id)
                await self.queue.ack(delivery)
                return
            taken_over = True
        try:
            identity = self._identity.load(message.identity)
        except (
            ValidationError
        ) as exc:  # the text names the fields and the rules, never the values: they are the identity
            fields = ", ".join(
                f"{'.'.join(str(part) for part in e['loc']) or 'identity'}: {e['type']}"
                for e in exc.errors(include_input=False, include_url=False)
            )
            await self._fail(
                row.job_id,
                InvalidJobIdentity(f"the identity of the message does not fit the declared type ({fields})"),
                delivery,
            )
            return
        except ValueError as exc:
            await self._fail(row.job_id, InvalidJobIdentity(str(exc)), delivery)
            return
        job = self._jobs.get(message.job_name)
        if job is None and self._jobs_lookup is not None:  # a name `jobs` does not have: ask the application
            try:
                job = await self._lookup(message.job_name, row.job_hash, identity)
            except JobLookupError as exc:
                await self._fail(row.job_id, exc, delivery)
                return
        if job is None:
            note = " (it is not in `jobs`, and `jobs_lookup` found nothing)" if self._jobs_lookup is not None else ""
            await self._fail(
                row.job_id, UnknownJob(f"This worker does not run a job called {message.job_name!r}{note}"), delivery
            )
            return
        try:
            job_input = job.input_model.model_validate(message.input)
        except ValidationError as exc:
            await self._fail(row.job_id, InvalidJobInput(str(exc)), delivery)
            return
        # Take the job in one atomic step: it succeeds only if the job is still queued and nobody asked to cancel it. A client
        # that cancels at the same moment loses or wins cleanly, and a job is never both cancelled and run.
        if not taken_over and not await self.db.start_job(row.job_id, worker_id=self.worker_id, lease=self.lease):
            current = await self.db.get_job(row.job_id)
            if current is not None and current.status is RunStatus.INIT and current.cancel_requested:
                await self.db.cancel_queued_job(row.job_id)  # asked to cancel before it started
            logger.info("worker_skipped job_id=%s: it was cancelled, or is not queued any more", row.job_id)
            await self.queue.ack(delivery)
            return
        started = await self.db.get_job(row.job_id)
        if started is None:  # deleted under us: nothing to run
            await self.queue.ack(delivery)
            return
        owner = JobOwner(worker_id=self.worker_id, executions=started.executions)
        if taken_over:
            logger.warning("worker_takeover job_id=%s execution=%d", row.job_id, owner.executions)
        task = asyncio.create_task(self._run_job(delivery, row, job, job_input, owner, identity))
        self._tasks[row.job_id] = task
        self._deliveries[row.job_id] = delivery
        self._owners[row.job_id] = owner
        task.add_done_callback(lambda _: self._ended(row.job_id))

    async def _lookup(self, name: str, job_hash: str | None, identity: Any) -> "type[Job] | None":
        """Ask `jobs_lookup` for a job it was given nothing about, and check the answer before anything runs."""
        assert self._jobs_lookup is not None
        try:
            found = await self._jobs_lookup(name, job_hash, identity)
        except Exception as exc:
            raise JobLookupError(f"jobs_lookup raised {type(exc).__name__}: {exc}") from exc
        if found is None:
            return None
        try:
            declared = as_job(found)
        except TypeError as exc:
            raise JobLookupError(f"jobs_lookup returned something that is not a job: {exc}") from exc
        if declared.name != name:
            raise JobLookupError(f"jobs_lookup was asked for {name!r} and returned a job called {declared.name!r}")
        if job_hash is not None and declared.job_hash() != job_hash:
            raise JobLookupError(
                f"the job {name!r} is not the one that was submitted: its hash is not the one recorded when it was"
            )
        return declared

    def _ended(self, job_id: str) -> None:
        self._tasks.pop(job_id, None)
        self._deliveries.pop(job_id, None)
        self._owners.pop(job_id, None)
        self._lost.discard(job_id)
        self._requeue.discard(job_id)
        self._wake.set()

    async def _fail(self, job_id: str, exc: BaseException, delivery: Delivery) -> None:
        error_type, error_hash = error_fields(exc)  # the text goes to the log, not the run log
        await self.db.set_job_status(job_id, RunStatus.FAILED, error_type=error_type, error_hash=error_hash)
        logger.error("job_failed job_id=%s error=%s: %s", job_id, error_type, exc)
        await self.queue.ack(delivery)

    async def _run_job(
        self,
        delivery: Delivery,
        row: JobRow,
        job: type[Job],
        job_input: BaseModel,
        owner: JobOwner,
        identity: Any = None,
    ) -> None:
        job_id = row.job_id

        async def on_cancel() -> None:
            if job_id in self._lost:  # it is not ours any more: write nothing, and the message is not ours to settle
                return
            try:
                if job_id in self._requeue:  # a shutdown: back to the queue, to run again
                    await self.db.set_job_status(job_id, RunStatus.INIT, owner=owner)
                    await self.queue.nack(delivery, requeue=True)
                    logger.warning("job_requeued job_id=%s", job_id)
                else:  # a client asked, or `cancel()` was called
                    await self.db.set_job_status(job_id, RunStatus.CANCELLED, owner=owner)
                    await self.queue.ack(delivery)
                    logger.warning("job_cancelled job_id=%s", job_id)
            except JobLost:
                self._log_lost(job_id)

        try:  # the same code as `run_job` runs a job with: the worker only adds the queue
            await execute_job(
                self.runner,
                job,
                job_id,
                job_input,
                self.results,
                on_cancel=on_cancel,
                trace_context=delivery.message.trace_context,
                owner=owner,
                lost=lambda: job_id in self._lost,
                identity=identity,
            )
        except asyncio.CancelledError:
            return  # `on_cancel` has settled the message and the status, or the job was lost
        except JobLost:
            self._log_lost(job_id)
            return  # a write found the job taken: it is not ours, so nothing is written and the message is not acknowledged
        await self.queue.ack(delivery)

    # the lease

    def _log_lost(self, job_id: str) -> None:
        owner = self._owners.get(job_id)
        logger.warning("job_lost job_id=%s execution=%s", job_id, owner.executions if owner else "?")

    def _lose(self, job_id: str) -> None:
        """The job was taken from this worker: stop it at once, quietly (nothing is written to it)."""
        if job_id in self._lost:
            return
        self._lost.add(job_id)
        self._log_lost(job_id)
        self.cancel(job_id)

    async def _heartbeat(self) -> None:
        """Every `heartbeat` seconds: renew, in one update, the lease of every job running here, and extend the queue's lease of
        its message. A job whose lease could not be renewed was taken from this worker: it is stopped."""
        while True:
            await asyncio.sleep(self.heartbeat)
            held = {job_id: owner.executions for job_id, owner in self._owners.items() if job_id not in self._lost}
            if not held:
                continue
            try:
                owned = await self.db.renew_job_leases(self.worker_id, held, self.lease)
            except Exception:
                logger.exception("worker_heartbeat_failed")
                continue
            for job_id in held.keys() - owned:
                self._lose(job_id)
            for job_id in owned:
                delivery = self._deliveries.get(job_id)
                if delivery is None:
                    continue
                try:
                    await self.queue.extend(delivery)
                except Exception:
                    logger.exception("worker_extend_failed job_id=%s", job_id)

    # cancelling and stopping

    async def _poll_cancels(self) -> None:
        """Every `cancel_poll` seconds, one query for all the jobs running here: cancel the ones a client asked to cancel."""
        while True:
            await asyncio.sleep(self.cancel_poll)
            if not self._tasks:
                continue
            try:
                for job_id in await self.db.list_cancel_requested(list(self._tasks)):
                    self.cancel(job_id)
            except Exception:
                logger.exception("worker_cancel_poll_failed")

    async def _shutdown(self, grace: float) -> None:
        """Wait up to `grace` for the running jobs, then cancel what is left and return it to the queue."""
        tasks = dict(self._tasks)
        if not tasks:
            return
        pending = set(tasks.values())
        if grace > 0:
            _, pending = await asyncio.wait(pending, timeout=grace)
        for job_id, task in tasks.items():
            if task in pending:
                self._requeue.add(job_id)
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
