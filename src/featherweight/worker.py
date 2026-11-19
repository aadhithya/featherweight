"""The job worker: the process that runs submitted jobs.

It receives messages from a queue, runs the job each one names, writes the output through the user's result store, and records
the outcome in the run log. It holds no state of its own beyond the tasks it is running: everything else is the database, the
queue and the user's store. A failed job is never retried here (the user resumes it). See docs/specs/submitted-jobs.md.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ValidationError

from featherweight.db import FINAL, Database, JobRow
from featherweight.fingerprint import error_fields, input_hash
from featherweight.jobs import Job, JobContext, as_job
from featherweight.models import AgentBase, RunStatus
from featherweight.queue import Delivery, JobQueue
from featherweight.results import ResultKey, ResultStore, SqliteResultStore
from featherweight.runner import AgentRunner
from featherweight.sqlite import SqliteDatabase

logger = logging.getLogger(__name__)


class UnknownJob(Exception):
    """A message named a job that this worker was not given."""


class InvalidJobInput(Exception):
    """The input of a message does not fit the input model of its job."""


class InvalidJobOutput(Exception):
    """A job returned something that is not an instance of its output model."""


class JobWorker:
    """`JobWorker(db, queue, runner, jobs, results)`: `jobs` are the `Job` classes (or plain agent classes) it can run, by name.

    `results` is where the output of a job is written (a `ResultStore`; the default is a sqlite store in the run log's file
    for a `SqliteDatabase`, and none for another database; `None` keeps nothing). `concurrency` is how many jobs run at once.

        worker = ftw.JobWorker(db, queue, runner, jobs=[Summarise])
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
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
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
        self._jobs: dict[str, type[Job]] = {}
        for job in jobs:
            declared = as_job(job)
            if declared.name in self._jobs and self._jobs[declared.name] is not declared:
                raise ValueError(f"Two jobs are called {declared.name!r}: a worker runs a job by its name")
            self._jobs[declared.name] = declared
        self._tasks: dict[str, asyncio.Task[None]] = {}  # the jobs running now, by job id
        self._deliveries: dict[str, Delivery] = {}
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
        try:
            await self._loop()
            await self._shutdown(self._grace)
        except asyncio.CancelledError:
            await self._shutdown(0)
            raise
        finally:
            poller.cancel()
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
        job = self._jobs.get(message.job_name)
        if job is None:
            await self._fail(
                row.job_id, UnknownJob(f"This worker does not run a job called {message.job_name!r}"), delivery
            )
            return
        try:
            job_input = job.input_model.model_validate(message.input)
        except ValidationError as exc:
            await self._fail(row.job_id, InvalidJobInput(str(exc)), delivery)
            return
        # Take the job in one atomic step: it succeeds only if the job is still queued and nobody asked to cancel it. A client
        # that cancels at the same moment loses or wins cleanly, and a job is never both cancelled and run. (A real redelivery
        # of a job that another worker is running is the next epic's.)
        if not await self.db.start_job(row.job_id):
            current = await self.db.get_job(row.job_id)
            if current is not None and current.status is RunStatus.INIT and current.cancel_requested:
                await self.db.cancel_queued_job(row.job_id)  # asked to cancel before it started
            logger.info("worker_skipped job_id=%s: it was cancelled, or is not queued any more", row.job_id)
            await self.queue.ack(delivery)
            return
        task = asyncio.create_task(self._run_job(delivery, row, job, job_input))
        self._tasks[row.job_id] = task
        self._deliveries[row.job_id] = delivery
        task.add_done_callback(lambda _: self._ended(row.job_id))

    def _ended(self, job_id: str) -> None:
        self._tasks.pop(job_id, None)
        self._deliveries.pop(job_id, None)
        self._requeue.discard(job_id)
        self._wake.set()

    async def _fail(self, job_id: str, exc: BaseException, delivery: Delivery) -> None:
        error_type, error_hash = error_fields(exc)  # the text goes to the log, not the run log
        await self.db.set_job_status(job_id, RunStatus.FAILED, error_type=error_type, error_hash=error_hash)
        logger.error("job_failed job_id=%s error=%s: %s", job_id, error_type, exc)
        await self.queue.ack(delivery)

    async def _run_job(self, delivery: Delivery, row: JobRow, job: type[Job], job_input: BaseModel) -> None:
        job_id = row.job_id
        logger.info("job_started job_id=%s job=%s", job_id, job.name)
        try:
            output = await job().run(JobContext(runner=self.runner, job_id=job_id), job_input)
            if not isinstance(output, job.output_model):
                raise InvalidJobOutput(f"{job.name} returned {type(output).__name__}, not {job.output_model.__name__}")
            await self._keep(row, job, job_input, output)
        except asyncio.CancelledError:
            if job_id in self._requeue:  # a shutdown: back to the queue, to run again
                await self.db.set_job_status(job_id, RunStatus.INIT)
                await self.queue.nack(delivery, requeue=True)
                logger.warning("job_requeued job_id=%s", job_id)
            else:  # a client asked, or `cancel()` was called
                await self.db.set_job_status(job_id, RunStatus.CANCELLED)
                await self.queue.ack(delivery)
                logger.warning("job_cancelled job_id=%s", job_id)
        except Exception as exc:
            await self._fail(job_id, exc, delivery)
        else:
            await self.db.set_job_status(job_id, RunStatus.SUCCESS)
            await self.queue.ack(delivery)
            logger.info("job_finished job_id=%s", job_id)

    async def _keep(self, row: JobRow, job: type[Job], job_input: BaseModel, output: BaseModel) -> None:
        """Write the output through the user's store. If it cannot be kept the job fails: the output is its product."""
        if self.results is None:
            return
        declared_hash = row.job_hash or job.job_hash()
        if row.job_hash and row.job_hash != job.job_hash():
            logger.warning(
                "job_hash_differs job_id=%s: this worker's %s is not the one that was submitted", row.job_id, job.name
            )
        key = ResultKey(job_id=row.job_id, agent_hash=declared_hash, input_hash=row.input_hash or input_hash(job_input))
        await self.results.put(key, output.model_dump(mode="json"))

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
