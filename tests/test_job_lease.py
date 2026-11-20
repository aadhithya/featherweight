"""The lease on a job: the owner is recorded, the heartbeat keeps a long job alive, and a worker that lost a job writes nothing."""

import asyncio

import pytest
from fakes import Reactive, runner_with
from pydantic import BaseModel

from featherweight import run_job
from featherweight.db import JobLost, JobOwner
from featherweight.fingerprint import input_hash
from featherweight.jobs import Job, JobContext
from featherweight.models import RunStatus
from featherweight.queue import JobMessage
from featherweight.testing import MemoryQueue, MemoryResultStore, RecordingObserver
from featherweight.worker import JobWorker


class In(BaseModel):
    text: str


class Out(BaseModel):
    shouted: str


class Hold(Job):
    """Runs until the test lets it go."""

    name = "hold"
    version = "1"
    input_model = In
    output_model = Out
    release: dict[str, asyncio.Event] = {}
    began: dict[str, asyncio.Event] = {}
    cancelled: list[str] = []

    async def run(self, ctx: JobContext, input: In) -> Out:
        Hold.began.setdefault(input.text, asyncio.Event()).set()
        try:
            await Hold.release.setdefault(input.text, asyncio.Event()).wait()
        except asyncio.CancelledError:
            Hold.cancelled.append(input.text)
            raise
        return Out(shouted=input.text.upper())


@pytest.fixture(autouse=True)
def fresh():
    Hold.release, Hold.began, Hold.cancelled = {}, {}, []


async def submit(db, queue, text):
    job_input = In(text=text)
    job_id = await db.create_job(Hold.name, job_hash=Hold.job_hash(), input_hash=input_hash(job_input))
    await queue.enqueue(JobMessage(job_id=job_id, job_name=Hold.name, input=job_input.model_dump(mode="json")))
    return job_id


def worker(db, queue, runner=None, **kwargs):
    kwargs.setdefault("receive_timeout", 0.05)
    kwargs.setdefault("cancel_poll", 0.05)
    runner = runner or runner_with(db, Reactive(respond=lambda *_: RuntimeError("no model"), delay_s=0))
    return JobWorker(db, queue, runner, [Hold], **kwargs)


async def until(condition, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.02)


async def running(db, job_id):
    row = await db.get_job(job_id)
    return row is not None and row.status is RunStatus.RUNNING


class Started:
    """`async with Started(w)`: the worker runs in a task, and is stopped at the end."""

    def __init__(self, w: JobWorker):
        self.worker = w

    async def __aenter__(self):
        self.task = asyncio.create_task(self.worker.run())
        return self.worker

    async def __aexit__(self, *exc):
        await self.worker.stop(grace=0.05)  # a job that is still held goes back to the queue
        await asyncio.wait_for(self.task, timeout=5)


# the owner


async def test_a_job_that_a_worker_runs_has_its_worker_a_lease_and_one_execution(db):
    queue = MemoryQueue()
    job_id = await submit(db, queue, "a")
    async with Started(worker(db, queue, worker_id="w1", lease=30)):
        await until(lambda: running(db, job_id))
        row = await db.get_job(job_id)
        assert row.worker_id == "w1" and row.executions == 1 and row.lease_expires_at is not None
    row = await db.get_job(job_id)  # the worker stops: the job goes back to the queue, unowned
    assert row.status is RunStatus.INIT and row.worker_id is None and row.lease_expires_at is None
    assert row.executions == 1, "going back to the queue keeps the count"


async def test_an_ended_job_has_no_lease_and_keeps_its_worker(db):
    queue = MemoryQueue()
    job_id = await submit(db, queue, "a")
    async with Started(worker(db, queue, worker_id="w1")):
        await until(lambda: running(db, job_id))
        Hold.release["a"].set()
        await until(lambda: _final(db, job_id))
    row = await db.get_job(job_id)
    assert (
        row.status is RunStatus.SUCCESS
        and row.worker_id == "w1"
        and row.lease_expires_at is None
        and row.executions == 1
    )


async def _final(db, job_id):
    row = await db.get_job(job_id)
    return row is not None and row.status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED)


async def test_a_job_run_in_process_has_an_owner_and_no_lease(db):
    class Quick(Job):
        name = "quick"
        version = "1"
        input_model = In
        output_model = Out

        async def run(self, ctx: JobContext, input: In) -> Out:
            return Out(shouted=input.text.upper())

    runner = runner_with(db, Reactive(respond=lambda *_: RuntimeError("no model"), delay_s=0))
    outcome = await run_job(runner, Quick, In(text="x"))
    row = await db.get_job(outcome.job_id)
    assert outcome.status is RunStatus.SUCCESS
    assert row.worker_id.startswith("process-") and row.executions == 1 and row.lease_expires_at is None


# the heartbeat


async def test_the_heartbeat_keeps_a_job_longer_than_its_lease_and_its_message_alive(db):
    from featherweight.queue import SqliteQueue

    queue = SqliteQueue(
        db, lease=1.0
    )  # a lease with room for a late heartbeat on a busy machine: it renews every third of it
    job_id = await submit(db, queue, "long")
    async with Started(worker(db, queue, lease=1.0)):  # renews every 0.33 s, and the lease has 0.67 s of slack
        await until(lambda: running(db, job_id))
        first = (await db.get_job(job_id)).lease_expires_at
        await asyncio.sleep(1.5)  # longer than the lease: without the heartbeat it would have expired
        row = await db.get_job(job_id)
        assert row.status is RunStatus.RUNNING and row.lease_expires_at > first, "the job lease moved forward"
        assert await queue.receive(0.05) is None, "the message lease was extended too: nobody else is given the message"
        Hold.release["long"].set()
        await until(lambda: _final(db, job_id))
    assert (await db.get_job(job_id)).status is RunStatus.SUCCESS
    assert await queue.receive(0.4) is None, "acknowledged: it does not come back"


# a job that was taken away


async def take_over(db, job_id, new_worker="other"):
    """What a takeover will do, by hand: the job goes back to the queue and another worker starts it."""
    await db.set_job_status(job_id, RunStatus.INIT)
    assert await db.start_job(job_id, worker_id=new_worker, lease=60) is True


async def test_a_worker_that_lost_a_job_stops_it_and_writes_nothing_and_keeps_its_other_jobs(db, monkeypatch):
    queue = MemoryQueue(lease=0.4)
    acked: list[tuple[str, int]] = []
    real_ack = queue.ack

    async def spy(delivery):
        acked.append((delivery.message.job_id, delivery.delivery_count))
        await real_ack(delivery)

    monkeypatch.setattr(queue, "ack", spy)
    lost = await submit(db, queue, "lost")
    kept = await submit(db, queue, "kept")
    observer = RecordingObserver()
    runner = runner_with(db, Reactive(respond=lambda *_: RuntimeError("no model"), delay_s=0), observers=[observer])
    async with Started(worker(db, queue, runner, lease=0.3)):
        await until(lambda: running(db, lost))
        await until(lambda: running(db, kept))
        await take_over(db, lost)  # another worker has the job now
        await until(lambda: _cancelled_in_job("lost"))
        Hold.release["kept"].set()
        await until(lambda: _final(db, kept))

        row = await db.get_job(lost)
        assert row.status is RunStatus.RUNNING and row.worker_id == "other" and row.executions == 2, (
            "the new owner's row is untouched"
        )
        assert [e.type for e in await db.list_job_events(lost)] == ["queued", "running", "queued", "running"], (
            "the old worker wrote no event"
        )
        assert (await db.get_job(kept)).status is RunStatus.SUCCESS, "the worker goes on with its other jobs"
        assert (kept, 1) in acked and (lost, 1) not in acked, "the lost job's delivery was not acknowledged"
    ends = [end for end in observer.records("job.finish") if end.error_type == "JobLost"]
    assert len(ends) == 1, "the observers were told, for tracing: the job's scope ended with error type JobLost"


async def _cancelled_in_job(text):
    return text in Hold.cancelled


async def test_a_stalled_worker_that_wakes_up_after_a_takeover_writes_nothing_and_stores_no_result(db):
    queue = MemoryQueue()
    store = MemoryResultStore()
    job_id = await submit(db, queue, "slow")
    # no heartbeat in time: the worker only finds out when it tries to write
    async with Started(worker(db, queue, results=store, lease=600, heartbeat=600)):
        await until(lambda: running(db, job_id))
        await take_over(db, job_id)
        Hold.release["slow"].set()  # the stalled job finishes
        await asyncio.sleep(0.3)
        row = await db.get_job(job_id)
        assert row.status is RunStatus.RUNNING and row.worker_id == "other" and row.executions == 2
        assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "queued", "running"]
        key_hash = Hold.job_hash()
        from featherweight.results import ResultKey

        assert (
            await store.get(ResultKey(job_id=job_id, agent_hash=key_hash, input_hash=input_hash(In(text="slow"))))
            is None
        ), "no result is stored for a job that is not the worker's any more"


async def test_every_conditional_write_is_refused_for_a_worker_that_does_not_own_the_job(db):
    job_id = await db.create_job("lease")
    assert await db.start_job(job_id, worker_id="w1", lease=60)
    stale = JobOwner(worker_id="w1", executions=2)
    for status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INIT):
        with pytest.raises(JobLost):
            await db.set_job_status(job_id, status, owner=stale)
    assert (await db.get_job(job_id)).status is RunStatus.RUNNING
    assert await db.renew_job_leases("w1", {job_id: 2}, 60) == set()


async def test_a_requeued_job_is_started_again_as_the_next_execution(db):
    queue = MemoryQueue()
    job_id = await submit(db, queue, "again")
    first = worker(db, queue, worker_id="w1")
    async with Started(first):
        await until(lambda: running(db, job_id))
    assert (await db.get_job(job_id)).status is RunStatus.INIT  # the shutdown put it back
    async with Started(worker(db, queue, worker_id="w2")):
        await until(lambda: running(db, job_id))
        row = await db.get_job(job_id)
        assert row.worker_id == "w2" and row.executions == 2


async def test_the_lease_options_are_checked(db):
    with pytest.raises(ValueError):
        worker(db, MemoryQueue(), lease=0)
    with pytest.raises(ValueError):
        worker(db, MemoryQueue(), heartbeat=-1)
    assert worker(db, MemoryQueue(), lease=30).heartbeat == 10
