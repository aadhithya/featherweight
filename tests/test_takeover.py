"""A worker that was lost: another takes its job over, as the next execution, and reuses the steps that finished. Fake models only."""

import asyncio
import json

import pytest
from fakes import Echo, EchoInput, Reactive, runner_with, structured_reply
from pydantic import BaseModel

from featherweight import JobClient
from featherweight.fingerprint import identity_hash, input_hash
from featherweight.jobs import Job, JobContext
from featherweight.models import Status
from featherweight.queue import DEAD_LETTERED, JobMessage, SqliteQueue
from featherweight.testing import MemoryQueue
from featherweight.worker import JobWorker


class In(BaseModel):
    text: str


class Out(BaseModel):
    done: str


class Teacher(BaseModel):
    user_id: str


class Steps(Job):
    """One agent step for each letter. The first execution hangs after the second step (`hold`), as a worker that stalls."""

    name = "steps"
    version = "1"
    input_model = In
    output_model = Out
    started = 0  # how many times it was started
    reached: asyncio.Event
    hold: asyncio.Event

    async def run(self, ctx: JobContext, input: In) -> Out:
        Steps.started += 1
        first = Steps.started == 1
        for position, letter in enumerate(input.text):
            await ctx.run(Echo, EchoInput(text=letter))
            if first and position == 1:
                Steps.reached.set()
                await Steps.hold.wait()  # the worker stalls here: it does not heartbeat
        return Out(done=input.text)


@pytest.fixture(autouse=True)
def fresh():
    Steps.started = 0
    Steps.reached, Steps.hold = asyncio.Event(), asyncio.Event()


def model() -> Reactive:
    def answer(user_text, call_number):
        return structured_reply("EchoOutput", {"echoed": json.loads(user_text)["text"]})

    return Reactive(respond=answer, delay_s=0)


def calls(fake: Reactive) -> dict[str, int]:
    return {json.loads(text)["text"]: count for text, count in sorted(fake.counts.items())}


async def submit(db, queue, text="abc", resumable=True, identity=None):
    job_input = In(text=text)
    job_id = await db.create_job(
        Steps.name, resumable=resumable, job_hash=Steps.job_hash(), input_hash=input_hash(job_input)
    )
    await queue.enqueue(
        JobMessage(
            job_id=job_id,
            job_name=Steps.name,
            input=job_input.model_dump(mode="json"),
            identity=identity.model_dump(mode="json") if identity else None,
            identity_kind=type(identity).__name__ if identity else None,
        )
    )
    return job_id


def worker(db, queue, fake, identity_type=None, **kwargs):
    kwargs.setdefault("receive_timeout", 0.05)
    kwargs.setdefault("cancel_poll", 0.05)
    runner = runner_with(db, fake, max_attempts=1, identity_type=identity_type)
    return JobWorker(db, queue, runner, [Steps], identity_type=identity_type, **kwargs)


async def until(condition, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.02)


async def final(db, job_id):
    row = await db.get_job(job_id)
    return row is not None and row.status is Status.SUCCESS


class Started:
    def __init__(self, w: JobWorker):
        self.worker = w

    async def __aenter__(self):
        self.task = asyncio.create_task(self.worker.run())
        return self.worker

    async def __aexit__(self, *exc):
        await self.worker.stop(grace=0.05)
        await asyncio.wait_for(self.task, timeout=5)


async def lost_and_taken_over(db, queue, fake, resumable=True, identity=None):
    """w1 starts the job and stalls (no heartbeat, a short lease); w2 gets the message back and takes the job over, and finishes it."""
    job_id = await submit(db, queue, resumable=resumable, identity=identity)
    kind = type(identity) if identity else None
    stalled = worker(db, queue, fake, kind, worker_id="w1", lease=0.3, heartbeat=3600, concurrency=1)
    async with Started(stalled):
        await asyncio.wait_for(Steps.reached.wait(), timeout=5)
        async with Started(worker(db, queue, fake, kind, worker_id="w2", lease=30)):
            await until(lambda: final(db, job_id))
            snapshot = (await db.get_job(job_id), await db.list_job_events(job_id), calls(fake))
            Steps.hold.set()  # the stalled worker wakes up
            await asyncio.sleep(0.3)
        await until(lambda: _idle(stalled))
    return job_id, snapshot


async def _idle(w: JobWorker):
    return not w._tasks


# the takeover


@pytest.mark.parametrize("make_queue", [lambda db: MemoryQueue(lease=0.3), lambda db: SqliteQueue(db, lease=0.3)])
async def test_a_job_whose_worker_stalled_is_taken_over_by_another_as_its_second_execution(db, make_queue):
    queue = make_queue(db)
    job_id, (row, events, _) = await lost_and_taken_over(db, queue, model())

    assert row.status is Status.SUCCESS and row.worker_id == "w2" and row.executions == 2
    assert [(e.type, e.status) for e in events] == [
        ("queued", Status.INIT),
        ("running", Status.RUNNING),
        ("lost", Status.INIT),
        ("running", Status.RUNNING),
        ("succeeded", Status.SUCCESS),
    ]


async def test_the_worker_that_was_lost_writes_nothing_when_it_wakes_up(db):
    queue = MemoryQueue(lease=0.3)
    job_id, (_, events, _) = await lost_and_taken_over(db, queue, model())

    assert await db.list_job_events(job_id) == events, "nothing was added to the job after the takeover"
    row = await db.get_job(job_id)
    assert row.status is Status.SUCCESS and row.worker_id == "w2" and row.executions == 2


async def test_a_takeover_of_a_resumable_job_reuses_the_steps_that_finished(db):
    fake = model()
    queue = MemoryQueue(lease=0.3)
    job_id, (_, _, seen) = await lost_and_taken_over(db, queue, fake)

    assert seen == {"a": 1, "b": 1, "c": 1}, "the steps that finished did not reach the model again"
    runs = await db.list_attempts(job_id=job_id)
    first = [r for r in runs if r.job_execution == 1][:2]
    second = [r for r in runs if r.job_execution == 2]
    assert len(first) == 2 and len(second) == 3
    assert [r.reused_from_attempt_id for r in second[:2]] == [r.attempt_id for r in first], (
        "the reused steps point at the runs of the first execution"
    )
    assert second[2].reused_from_attempt_id is None, "the third step ran"
    assert all(r.reused_from_attempt_id not in {s.attempt_id for s in second} for r in second), (
        "no run reuses its own execution's"
    )
    assert [(r.input_tokens, r.output_tokens) for r in second[:2]] == [(0, 0), (0, 0)]


async def test_a_takeover_of_a_job_for_an_identity_reuses_the_steps_of_the_same_identity(db):
    fake = model()
    teacher = Teacher(user_id="u-1")
    job_id, (_, _, seen) = await lost_and_taken_over(db, MemoryQueue(lease=0.3), fake, identity=teacher)

    assert seen == {"a": 1, "b": 1, "c": 1}, "the steps that finished did not reach the model again"
    runs = await db.list_attempts(job_id=job_id)
    assert {r.identity_hash for r in runs} == {identity_hash(teacher)}, (
        "both executions ran for the identity of the message"
    )
    second = [r for r in runs if r.job_execution == 2]
    assert [r.reused_from_attempt_id is not None for r in second] == [True, True, False]


async def test_a_takeover_of_a_job_that_is_not_resumable_runs_every_step_again(db):
    fake = model()
    queue = MemoryQueue(lease=0.3)
    job_id, (_, _, seen) = await lost_and_taken_over(db, queue, fake, resumable=False)

    assert seen == {"a": 2, "b": 2, "c": 1}, "a job that kept no results runs from the start"
    assert all(r.reused_from_attempt_id is None for r in await db.list_attempts(job_id=job_id))


async def test_a_message_that_comes_back_to_the_worker_that_runs_the_job_is_a_duplicate(db):
    queue = MemoryQueue(lease=0.2)
    job_id = await submit(db, queue)
    async with Started(worker(db, queue, model(), worker_id="w1", lease=0.2, heartbeat=3600, concurrency=2)):
        await asyncio.wait_for(Steps.reached.wait(), timeout=5)
        await asyncio.sleep(0.6)  # the message comes back to w1, which has a free slot, and the lease is long over
        row = await db.get_job(job_id)
        assert row.worker_id == "w1" and row.executions == 1, "a worker does not take over the job that it runs"
        Steps.hold.set()
        await until(lambda: final(db, job_id))
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "succeeded"]


async def test_a_duplicate_delivery_of_a_job_that_is_alive_is_skipped(db):
    queue = MemoryQueue()
    fake = model()
    job_id = await submit(db, queue)
    async with Started(worker(db, queue, fake, worker_id="w1", lease=30)):
        await asyncio.wait_for(Steps.reached.wait(), timeout=5)
        await queue.enqueue(
            JobMessage(job_id=job_id, job_name=Steps.name, input=In(text="abc").model_dump(mode="json"))
        )  # the same job again
        async with Started(worker(db, queue, fake, worker_id="w2", lease=30)):
            await asyncio.sleep(0.4)
            row = await db.get_job(job_id)
            assert row.worker_id == "w1" and row.executions == 1 and row.status is Status.RUNNING
            assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running"]
        assert await queue.receive(0.05) is None, "the duplicate was acknowledged"
        Steps.hold.set()
        await until(lambda: final(db, job_id))
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "succeeded"]


# ending a job that will not run


async def test_fail_ends_a_job_that_has_not_started_and_leaves_a_final_one(db):
    client = JobClient(db, MemoryQueue())
    queued = await db.create_job("p")
    assert await client.fail(queued) is True
    row = await db.get_job(queued)
    assert row.status is Status.FAILED and row.error_type == DEAD_LETTERED
    events = await db.list_job_events(queued)
    assert [(e.type, e.error_type) for e in events] == [("queued", None), ("failed", DEAD_LETTERED)]
    assert await client.fail(queued) is False, "a job that ended stays as it is"
    assert await client.fail("no-such-job") is False

    done = await db.create_job("p")
    await db.set_job_status(done, Status.SUCCESS)
    assert await client.fail(done) is False
    assert (await db.get_job(done)).status is Status.SUCCESS

    running = await db.create_job("p")
    assert await db.start_job(running, worker_id="w1", lease=30) is True
    assert await client.fail(running) is False, "a job that runs with a live lease is not failed"


async def test_a_message_that_the_sqlite_queue_gave_up_on_fails_its_job(db):
    queue = SqliteQueue(db, lease=0.05, max_deliveries=1)
    job_id = await db.create_job("p")
    await queue.enqueue(JobMessage(job_id=job_id, job_name="p", input={}))
    first = await queue.receive(1)
    assert first is not None and first.delivery_count == 1
    await asyncio.sleep(0.15)  # its lease ends, and it was delivered as often as allowed
    assert await queue.receive(0.05) is None

    row = await db.get_job(job_id)
    assert row.status is Status.FAILED and row.error_type == DEAD_LETTERED
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "failed"]


async def test_a_message_that_was_given_up_on_while_its_worker_was_lost_fails_the_running_job(db):
    queue = SqliteQueue(db, lease=0.05, max_deliveries=1)
    job_id = await db.create_job("p")
    assert await db.start_job(job_id, worker_id="w1", lease=0.05) is True  # the worker died on its only delivery
    await queue.enqueue(JobMessage(job_id=job_id, job_name="p", input={}))
    assert await queue.receive(1) is not None
    await asyncio.sleep(0.15)
    assert await queue.receive(0.05) is None
    row = await db.get_job(job_id)
    assert row.status is Status.FAILED and row.error_type == DEAD_LETTERED
