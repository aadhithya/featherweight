"""Observers and jobs: the job scope, for `run_job` and for a worker alike, and the trace context across the queue."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from fakes import Echo, EchoInput, Scripted, runner_with, structured_reply
from pydantic import BaseModel

from featherweight import JobClient, JobWorker, Observer, SqliteQueue, run_job
from featherweight.events import wait_for_job
from featherweight.fingerprint import input_hash
from featherweight.jobs import Job, JobContext
from featherweight.models import Status
from featherweight.observers import JobEnd, JobStart, JobSubmitted, Scope
from featherweight.queue import JobMessage
from featherweight.testing import MemoryQueue, MemoryResultStore, RecordingContentObserver, RecordingObserver


class In(BaseModel):
    text: str


class Out(BaseModel):
    shouted: str


class Shout(Job):
    name = "shout"
    version = "3"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        result = await ctx.run(Echo, EchoInput(text=input.text))  # one run inside the job
        return Out(shouted=result.output.echoed.upper())


class Boom(Job):
    name = "boom"
    version = "1"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        raise RuntimeError(f"secret failure about {input.text}")


def echoed(text="hello"):
    return structured_reply("EchoOutput", {"echoed": text})


def runner(db, *observers, text="hello"):
    return runner_with(db, Scripted(script=[echoed(text)]), observers=list(observers))


async def worker_runs(db, queue, run, job, message_text="hello"):
    """What a client does, by hand, and a worker that runs it: returns the job id once it is final."""
    job_input = In(text=message_text)
    job_id = await db.create_job(job.name, job_hash=job.job_hash(), input_hash=input_hash(job_input))
    await queue.enqueue(JobMessage(job_id=job_id, job_name=job.name, input=job_input.model_dump(mode="json")))
    worker = JobWorker(db, queue, run, [job])
    task = asyncio.create_task(worker.run())
    try:
        await wait_for_job(db, job_id, timeout=5)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(task, timeout=5)
    return job_id


# the job scope


async def test_run_job_wraps_the_whole_job_and_finishes_it(db):
    seen = RecordingObserver()

    outcome = await run_job(runner(db, seen), Shout, In(text="hello"))

    assert outcome.status is Status.SUCCESS
    names = seen.names()
    assert names[0] == "job.enter" and names[-2:] == ["job.finish", "job.exit"]
    assert "attempt.enter" in names and names.index("attempt.exit") < names.index(
        "job.finish"
    )  # the agent ran inside it
    start, end = seen.records("job.enter")[0], seen.records("job.finish")[0]
    row = await db.get_job(outcome.job_id)
    assert start == JobStart(
        job_id=outcome.job_id,
        name="shout",
        version="3",
        job_hash=Shout.job_hash(),
        input_hash=input_hash(In(text="hello")),
        resumable=False,
        resumed_from=None,
        execution=1,
        trace_context={},
        input=None,
    )
    assert row is not None and row.status is Status.SUCCESS
    assert end == JobEnd(status=Status.SUCCESS)  # no content for an observer that did not ask


async def test_a_worker_reports_the_same_job_as_run_job_does(db):
    in_process, on_worker = RecordingObserver(), RecordingObserver()

    outcome = await run_job(runner(db, in_process), Shout, In(text="hello"))
    job_id = await worker_runs(db, MemoryQueue(), runner(db, on_worker), Shout)

    def shape(seen: RecordingObserver, job_id: str):
        (start,) = seen.records("job.enter")
        (end,) = seen.records("job.finish")
        return start.model_copy(update={"job_id": "-"}), end, [n for n in seen.names() if n.startswith("job")]

    assert outcome.job_id != job_id
    assert shape(in_process, outcome.job_id) == shape(on_worker, job_id)


async def test_a_failed_job_ends_its_scope_with_the_error_type_and_content_only_if_asked(db):
    plain, content = RecordingObserver(), RecordingContentObserver()

    outcome = await run_job(runner(db, plain, content), Boom, In(text="pw"))

    assert outcome.status is Status.FAILED
    (plain_start,), (plain_end,) = plain.records("job.enter"), plain.records("job.finish")
    assert plain_end == JobEnd(status=Status.FAILED, error_type="RuntimeError") and plain_start.input is None
    (content_start,), (content_end,) = content.records("job.enter"), content.records("job.finish")
    assert content_start.input == {"text": "pw"}
    assert content_end.error == "RuntimeError: secret failure about pw"


async def test_a_successful_job_gives_its_output_to_an_observer_that_asks(db):
    content = RecordingContentObserver()

    await run_job(runner(db, content), Shout, In(text="hello"))

    assert content.records("job.finish")[0].output == {"shouted": "HELLO"}


async def test_a_cancelled_job_ends_its_scope_as_cancelled(db):
    seen = RecordingObserver()
    release = asyncio.Event()

    class Hang(Job):
        name = "hang"
        version = "1"
        input_model = In
        output_model = Out

        async def run(self, ctx: JobContext, input: In) -> Out:
            await release.wait()
            return Out(shouted="")

    task = asyncio.create_task(run_job(runner(db, seen), Hang, In(text="x")))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "job.enter" in seen.names():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen.records("job.finish")[0].status is Status.CANCELLED
    assert seen.names()[-1] == "job.exit"


async def test_a_resumed_job_tells_its_observer_what_it_resumes(db):
    seen = RecordingObserver()
    first = await db.create_job("shout", resumable=True)

    outcome = await run_job(runner(db, seen), Shout, In(text="hello"), resume_from=first)

    (start,) = seen.records("job.enter")
    assert (start.job_id, start.resumed_from, start.resumable) == (outcome.job_id, first, True)


async def test_no_observer_means_no_change(db):
    outcome = await run_job(runner(db), Shout, In(text="hello"))

    assert outcome.status is Status.SUCCESS and outcome.result == Out(shouted="HELLO")


# an observer never changes a job


class Breaks(Observer):
    """Raises in every place that a job, a client and an attempt call it."""

    def job(self, start: JobStart) -> AbstractAsyncContextManager[Scope]:
        raise RuntimeError("no scope for you")

    def attempt(self, start) -> AbstractAsyncContextManager[Scope]:
        @asynccontextmanager
        async def broken() -> AsyncGenerator[Scope]:
            raise RuntimeError("broken scope")
            yield Scope()

        return broken()

    async def submitted(self, job: JobSubmitted):
        raise RuntimeError("no carrier for you")


async def test_an_observer_that_raises_changes_nothing_for_a_job_a_worker_or_a_client(db):
    outcome = await run_job(runner(db, Breaks()), Shout, In(text="hello"))
    assert outcome.status is Status.SUCCESS and outcome.result == Out(shouted="HELLO")

    queue = MemoryQueue()
    client = JobClient(db, queue, MemoryResultStore(), observers=[Breaks()])
    job_id = await client.submit(Shout, In(text="hello"))
    delivery = await queue.receive(0.05)
    assert delivery is not None and delivery.message.job_id == job_id
    assert delivery.message.trace_context == {}  # the failing hook gave nothing
    await queue.nack(delivery, requeue=True)
    await worker_runs(db, MemoryQueue(), runner(db, Breaks()), Shout)


# the trace context across the queue


async def test_the_carrier_of_a_clients_observer_travels_in_the_message_and_reaches_the_workers_observer(db):
    carrier = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    client_side, worker_side = RecordingObserver(), RecordingObserver()
    client_side.carrier = carrier
    queue = SqliteQueue(db)
    client = JobClient(db, queue, MemoryResultStore(), observers=[client_side])

    job_id = await client.submit(Shout, In(text="hello"))

    (submitted,) = client_side.records("submitted")
    assert (submitted.job_id, submitted.name, submitted.version) == (job_id, "shout", "3")
    assert submitted.job_hash == Shout.job_hash() and submitted.input is None  # no content: it was not asked for

    worker = JobWorker(db, queue, runner(db, worker_side), [Shout])
    task = asyncio.create_task(worker.run())
    try:
        await wait_for_job(db, job_id, timeout=5)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(task, timeout=5)

    (start,) = worker_side.records("job.enter")
    assert start.trace_context == carrier and start.job_id == job_id


@pytest.mark.parametrize("make_queue", [MemoryQueue, SqliteQueue], ids=["memory", "sqlite"])
async def test_the_message_holds_the_carrier_until_it_is_acknowledged(db, make_queue):
    queue = MemoryQueue() if make_queue is MemoryQueue else SqliteQueue(db)
    seen = RecordingObserver()
    seen.carrier = {"traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01", "tracestate": "k=v"}

    await JobClient(db, queue, None, observers=[seen]).submit(Shout, In(text="x"))

    delivery = await queue.receive(0.05)
    assert delivery is not None and delivery.message.trace_context == seen.carrier
    await queue.ack(delivery)


async def test_a_client_without_observers_sends_an_empty_trace_context(db):
    queue = MemoryQueue()

    await JobClient(db, queue, None).submit(Shout, In(text="x"))

    delivery = await queue.receive(0.05)
    assert delivery is not None and delivery.message.trace_context == {}


async def test_the_carriers_of_several_observers_are_merged_and_content_goes_only_to_those_that_ask(db):
    first, second = RecordingObserver(), RecordingContentObserver()
    first.carrier, second.carrier = {"a": "1"}, {"b": "2"}
    queue = MemoryQueue()

    await JobClient(db, queue, None, observers=[first, second]).submit(Shout, In(text="secret"))

    delivery = await queue.receive(0.05)
    assert delivery is not None and delivery.message.trace_context == {"a": "1", "b": "2"}
    assert first.records("submitted")[0].input is None
    assert second.records("submitted")[0].input == {"text": "secret"}


async def test_nothing_about_a_trace_is_stored_in_the_run_log(db):
    seen = RecordingObserver()
    seen.carrier = {"traceparent": "00-cccccccccccccccccccccccccccccccc-dddddddddddddddd-01"}
    queue = SqliteQueue(db)
    client = JobClient(db, queue, MemoryResultStore(), observers=[seen])
    job_id = await client.submit(Shout, In(text="hello"))
    worker = JobWorker(db, queue, runner(db), [Shout])
    task = asyncio.create_task(worker.run())
    try:
        await wait_for_job(db, job_id, timeout=5)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(task, timeout=5)

    async with db.conn.execute("SELECT * FROM jobs") as cur:
        job_rows = [dict(r) for r in await cur.fetchall()]
    async with db.conn.execute("SELECT * FROM attempts") as cur:
        run_rows = [dict(r) for r in await cur.fetchall()]
    async with db.conn.execute("SELECT * FROM job_events") as cur:
        event_rows = [dict(r) for r in await cur.fetchall()]
    stored = repr((job_rows, run_rows, event_rows))
    assert "traceparent" not in stored and "cccccccc" not in stored
    async with db.conn.execute("SELECT COUNT(*) FROM job_queue") as cur:  # the message was acknowledged away
        assert (await cur.fetchone())[0] == 0
