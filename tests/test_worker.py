"""The worker: what it does with each message, cancel, shutdown and concurrency. Fake models only."""

import asyncio
import json
import logging

import pytest
from fakes import Echo, EchoInput, Reactive, runner_with, structured_reply
from pydantic import BaseModel

from featherweight.db import Database
from featherweight.events import wait_for_job
from featherweight.fingerprint import error_fields, input_hash
from featherweight.jobs import Job, JobContext
from featherweight.models import Status
from featherweight.queue import JobMessage, JobQueue, SqliteQueue
from featherweight.results import ResultKey, SqliteResultStore
from featherweight.sqlite import SqliteDatabase
from featherweight.testing import MemoryQueue, MemoryResultStore
from featherweight.worker import JobWorker


class In(BaseModel):
    text: str


class Out(BaseModel):
    shouted: str


class Shout(Job):
    name = "shout"
    version = "1"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        return Out(shouted=input.text.upper())


class Boom(Job):
    name = "boom"
    version = "1"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        raise RuntimeError(f"secret failure about {input.text}")


class WrongOutput(Job):
    name = "wrong"
    version = "1"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        return In(text="not an Out")


class Gate:
    """Lets a test hold jobs and count how many run at once."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.running = 0
        self.most = 0
        self.began: list[str] = []


GATE = Gate()


class Slow(Job):
    name = "slow"
    version = "1"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        GATE.running += 1
        GATE.most = max(GATE.most, GATE.running)
        GATE.began.append(input.text)
        GATE.started.set()
        try:
            await GATE.release.wait()
        finally:
            GATE.running -= 1
        return Out(shouted=input.text)


@pytest.fixture(autouse=True)
def fresh_gate():
    global GATE
    GATE = Gate()


async def submit(db: Database, queue: JobQueue, job: type[Job], text: str = "hi") -> str:
    """What the client does (it is built elsewhere): create the job row, and put the message on the queue."""
    job_input = In(text=text)
    job_id = await db.create_job(job.name, job_hash=job.job_hash(), input_hash=input_hash(job_input))
    await queue.enqueue(JobMessage(job_id=job_id, job_name=job.name, input=job_input.model_dump(mode="json")))
    return job_id


def make(db, queue, jobs, **kwargs):
    kwargs.setdefault("receive_timeout", 0.05)
    kwargs.setdefault("cancel_poll", 0.05)
    runner = kwargs.pop("runner", None) or runner_with(
        db, Reactive(respond=lambda *_: RuntimeError("no model"), delay_s=0)
    )
    return JobWorker(db, queue, runner, jobs, **kwargs)


class Running:
    """`async with Running(worker)`: the worker runs in a task, and is stopped at the end."""

    def __init__(self, worker: JobWorker):
        self.worker = worker

    async def __aenter__(self):
        self.task = asyncio.create_task(self.worker.run())
        return self.worker

    async def __aexit__(self, *exc):
        await self.worker.stop(grace=1)
        await asyncio.wait_for(self.task, timeout=5)


async def final(db, job_id):
    return await wait_for_job(db, job_id, timeout=5)


@pytest.fixture(params=["memory", "sqlite"])
def queue(request, db):
    return MemoryQueue() if request.param == "memory" else SqliteQueue(db)


# success and failure


async def test_a_job_runs_its_output_is_kept_and_its_message_is_gone(db, queue):
    store = MemoryResultStore()
    job_id = await submit(db, queue, Shout, "hello")
    async with Running(make(db, queue, [Shout], results=store)):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS and row.started_at is not None and row.completed_at is not None
    key = ResultKey(job_id=job_id, agent_hash=Shout.job_hash(), input_hash=input_hash(In(text="hello")))
    assert await store.get(key) == {"shouted": "HELLO"}
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "succeeded"]
    assert await queue.receive(0.05) is None  # acknowledged: it does not come back


async def test_the_default_store_of_a_sqlite_run_log_keeps_the_output_in_the_same_file(db):
    queue = MemoryQueue()
    job_id = await submit(db, queue, Shout, "x")
    async with Running(make(db, queue, [Shout])) as worker:
        await final(db, job_id)
    assert isinstance(worker.results, SqliteResultStore)
    key = ResultKey(job_id=job_id, agent_hash=Shout.job_hash(), input_hash=input_hash(In(text="x")))
    assert await worker.results.get(key) == {"shouted": "X"}


async def test_a_failed_job_is_failed_with_a_type_and_a_hash_and_is_not_retried(db, queue, caplog):
    store = MemoryResultStore()
    job_id = await submit(db, queue, Boom, "pw")
    with caplog.at_level(logging.ERROR, logger="featherweight"):
        async with Running(make(db, queue, [Boom], results=store)):
            row = await final(db, job_id)
            await asyncio.sleep(0.2)  # a retry would show up now

    assert row.status is Status.FAILED
    assert (row.error_type, row.error_hash) == error_fields(RuntimeError("secret failure about pw"))
    assert "secret failure about pw" in caplog.text  # the text is in the log, not the run log
    assert (await db.get_job(job_id)).status is Status.FAILED
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "failed"]
    assert await queue.receive(0.05) is None and store._results == {}


async def test_an_agent_can_be_run_as_a_job(db, queue):
    def answer(user_text, call_number):
        return structured_reply("EchoOutput", {"echoed": json.loads(user_text)["text"]})

    runner = runner_with(db, Reactive(respond=answer, delay_s=0), max_attempts=1)
    store = MemoryResultStore()
    job_input = EchoInput(text="a")
    from featherweight.fingerprint import agent_hash
    from featherweight.jobs import as_job

    job_id = await db.create_job("echo", job_hash=agent_hash(Echo()), input_hash=input_hash(job_input))
    await queue.enqueue(JobMessage(job_id=job_id, job_name="echo", input={"text": "a"}))
    async with Running(make(db, queue, [Echo], runner=runner, results=store)):
        row = await final(db, job_id)
    assert row.status is Status.SUCCESS and as_job(Echo).name == "echo"
    assert (await db.list_attempts(job_id=job_id))[0].job_id == job_id  # the agent ran in the job
    key = ResultKey(job_id=job_id, agent_hash=agent_hash(Echo()), input_hash=input_hash(job_input))
    assert (await store.get(key) or {}).get("echoed") == "a"


async def test_a_job_that_runs_agents_through_the_context_carries_its_id(db, queue):
    def answer(user_text, call_number):
        return structured_reply("EchoOutput", {"echoed": json.loads(user_text)["text"]})

    class UsesAgents(Job):
        name = "uses-agents"
        version = "1"
        input_model = In
        output_model = Out

        async def run(self, ctx: JobContext, input: In) -> Out:
            results = await ctx.run_many(Echo, [EchoInput(text=input.text), EchoInput(text="b")])
            return Out(shouted="".join(r.output.echoed for r in results if r.output))

    runner = runner_with(db, Reactive(respond=answer, delay_s=0), max_attempts=1)
    job_id = await submit(db, queue, UsesAgents, "a")
    async with Running(make(db, queue, [UsesAgents], runner=runner, results=None)):
        await final(db, job_id)
    assert [r.job_id for r in await db.list_attempts()] == [job_id, job_id]


# what the worker refuses


async def test_an_unknown_job_name_fails_the_job_and_acknowledges(db, queue):
    job_id = await submit(db, queue, Shout)
    async with Running(make(db, queue, [Boom])):
        row = await final(db, job_id)
    assert row.status is Status.FAILED and row.error_type == "UnknownJob"
    assert await queue.receive(0.05) is None


async def test_an_input_that_does_not_fit_fails_the_job_and_acknowledges(db, queue):
    job_id = await db.create_job("shout")
    await queue.enqueue(JobMessage(job_id=job_id, job_name="shout", input={"wrong": 1}))
    async with Running(make(db, queue, [Shout])):
        row = await final(db, job_id)
    assert row.status is Status.FAILED and row.error_type == "InvalidJobInput"
    assert await queue.receive(0.05) is None


async def test_an_output_that_is_not_the_output_model_fails_the_job(db, queue):
    job_id = await submit(db, queue, WrongOutput)
    async with Running(make(db, queue, [WrongOutput])):
        row = await final(db, job_id)
    assert row.status is Status.FAILED and row.error_type == "InvalidJobOutput"


async def test_a_store_that_cannot_keep_the_output_fails_the_job(db, queue):
    class Failing(MemoryResultStore):
        async def put(self, key, output):
            raise OSError("disk full")

    job_id = await submit(db, queue, Shout)
    async with Running(make(db, queue, [Shout], results=Failing())):
        row = await final(db, job_id)
    assert row.status is Status.FAILED and row.error_type == "OSError"  # the output is the job's product


async def test_no_store_keeps_nothing_and_the_job_still_succeeds(db, queue):
    job_id = await submit(db, queue, Shout)
    async with Running(make(db, queue, [Shout], results=None)):
        assert (await final(db, job_id)).status is Status.SUCCESS


async def test_a_job_that_is_already_cancelled_is_skipped_and_acknowledged(db, queue):
    job_id = await submit(db, queue, Slow)
    await db.set_job_status(job_id, Status.CANCELLED)
    async with Running(make(db, queue, [Slow])):
        await asyncio.sleep(0.3)
    assert GATE.began == [] and await queue.receive(0.05) is None
    assert (await db.get_job(job_id)).status is Status.CANCELLED


async def test_a_job_asked_to_cancel_before_it_started_is_cancelled_not_run(db, queue):
    job_id = await submit(db, queue, Slow)
    assert await db.request_job_cancel(job_id)
    async with Running(make(db, queue, [Slow])):
        row = await final(db, job_id)
    assert row.status is Status.CANCELLED and GATE.began == []


async def test_a_job_cancelled_in_the_instant_before_the_worker_takes_it_is_not_run(db, queue, monkeypatch):
    job_id = await submit(db, queue, Slow)
    real = db.start_job

    async def a_client_gets_in_first(job_id, **owner):
        assert await db.cancel_queued_job(job_id) is True  # the client's cancel lands just before the worker's take
        return await real(job_id, **owner)  # which then finds the job is not queued any more

    monkeypatch.setattr(db, "start_job", a_client_gets_in_first)
    async with Running(make(db, queue, [Slow])):
        row = await final(db, job_id)
    assert row.status is Status.CANCELLED and GATE.began == [] and await queue.receive(0.05) is None
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "cancelled"]  # it never ran


def test_two_jobs_with_one_name_are_refused(db):
    class Other(Job):
        name = "shout"
        version = "2"
        input_model = In
        output_model = Out

        async def run(self, ctx, input):
            return Out(shouted="")

    with pytest.raises(ValueError, match="Two jobs are called 'shout'"):
        JobWorker(db, MemoryQueue(), None, [Shout, Other])
    with pytest.raises(ValueError, match="concurrency"):
        JobWorker(db, MemoryQueue(), None, [Shout], concurrency=0)


# cancelling


async def test_cancel_in_the_same_process_cancels_the_running_job_at_once(db, queue):
    job_id = await submit(db, queue, Slow)
    async with Running(make(db, queue, [Slow], cancel_poll=60)) as worker:
        await asyncio.wait_for(GATE.started.wait(), timeout=5)
        assert worker.cancel(job_id) is True and worker.cancel("nope") is False
        row = await final(db, job_id)
    assert row.status is Status.CANCELLED
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "cancelled"]
    assert await queue.receive(0.05) is None  # acknowledged


async def test_cancel_from_another_process_is_found_by_the_poll(tmp_path):
    path = tmp_path / "shared.db"
    async with SqliteDatabase(path) as worker_db, SqliteDatabase(path) as client_db:
        queue = SqliteQueue(worker_db)
        job_id = await submit(worker_db, queue, Slow)
        async with Running(make(worker_db, queue, [Slow], cancel_poll=0.05)):
            await asyncio.wait_for(GATE.started.wait(), timeout=5)
            assert await client_db.request_job_cancel(job_id) is True  # a client, with a connection of its own
            row = await final(worker_db, job_id)
        assert row.status is Status.CANCELLED
        assert "cancel_requested" in [e.type for e in await worker_db.list_job_events(job_id)]


# concurrency


async def test_it_never_runs_more_jobs_at_once_than_the_concurrency(db, queue):
    ids = [await submit(db, queue, Slow, str(n)) for n in range(5)]
    async with Running(make(db, queue, [Slow], concurrency=2)):
        await asyncio.sleep(0.4)
        assert GATE.running == 2 and len(GATE.began) == 2  # the others wait on the queue
        GATE.release.set()
        rows = [await final(db, job_id) for job_id in ids]
    assert all(r.status is Status.SUCCESS for r in rows) and GATE.most == 2


# shutdown


async def test_stop_waits_for_a_job_that_finishes_within_the_grace(db, queue):
    job_id = await submit(db, queue, Slow)
    worker = make(db, queue, [Slow])
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(GATE.started.wait(), timeout=5)
    stopping = asyncio.create_task(worker.stop(grace=5))
    await asyncio.sleep(0.2)
    assert not stopping.done()  # it is waiting for the job
    GATE.release.set()
    await asyncio.wait_for(stopping, timeout=5)
    await asyncio.wait_for(task, timeout=5)
    assert (await db.get_job(job_id)).status is Status.SUCCESS


async def test_stop_cancels_a_job_that_outlasts_the_grace_and_returns_it_to_the_queue(db, queue):
    job_id = await submit(db, queue, Slow)
    worker = make(db, queue, [Slow])
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(GATE.started.wait(), timeout=5)
    await worker.stop(grace=0.1)
    await asyncio.wait_for(task, timeout=5)

    row = await db.get_job(job_id)
    assert row is not None and row.status is Status.INIT and row.started_at is None  # back in the queue
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "queued"]
    again = await queue.receive(1.0)
    assert again is not None and again.message.job_id == job_id  # the message is receivable again

    GATE.release.set()  # and another worker runs it from the start
    await queue.nack(again, requeue=True)
    async with Running(make(db, queue, [Slow])):
        assert (await final(db, job_id)).status is Status.SUCCESS


async def test_a_worker_whose_task_is_cancelled_returns_its_jobs_to_the_queue(db, queue):
    job_id = await submit(db, queue, Slow)
    worker = make(db, queue, [Slow])
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(GATE.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await db.get_job(job_id)).status is Status.INIT
    assert (await queue.receive(1.0)) is not None


async def test_a_worker_can_be_stopped_when_it_is_not_running_and_cannot_run_twice(db):
    worker = make(db, MemoryQueue(), [Shout])
    await worker.stop()  # nothing to stop
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    with pytest.raises(RuntimeError, match="already running"):
        await worker.run()
    await worker.stop(grace=1)
    await task
