"""JobClient: submit, status, cancel, result, events and wait. The worker is not here: a small consumer plays it. Fake models only."""

import asyncio

import pytest
from fakes import Echo, EchoInput, EchoOutput
from pydantic import BaseModel

import featherweight as ftw
from featherweight.client import JobClient
from featherweight.db import Database
from featherweight.fingerprint import error_fields, input_hash
from featherweight.jobs import Job, JobContext
from featherweight.models import RunStatus
from featherweight.queue import JobMessage, JobQueue, SqliteQueue
from featherweight.results import ResultKey, SqliteResultStore
from featherweight.sqlite import SqliteDatabase
from featherweight.streaming import StreamingAgent
from featherweight.testing import MemoryQueue, MemoryResultStore


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


@pytest.fixture(params=["memory", "sqlite"])
def queue(request, db) -> JobQueue:
    return MemoryQueue() if request.param == "memory" else SqliteQueue(db)


async def consume(db: Database, queue: JobQueue, results=None, *, finish: RunStatus = RunStatus.SUCCESS) -> JobMessage:
    """What a worker does, in a few lines: take a message, run the job, keep its result, set the statuses, acknowledge."""
    delivery = await queue.receive(2.0)
    assert delivery is not None, "no message arrived"
    message = delivery.message
    await db.set_job_status(message.job_id, RunStatus.RUNNING)
    if finish is RunStatus.SUCCESS:
        job = await db.get_job(message.job_id)
        assert job is not None and job.job_hash and job.input_hash
        if results is not None:
            output = Out(shouted=message.input["text"].upper())
            await results.put(
                ResultKey(job_id=job.job_id, agent_hash=job.job_hash, input_hash=job.input_hash), output.model_dump()
            )
        await db.set_job_status(message.job_id, RunStatus.SUCCESS)
    else:
        await db.set_job_status(message.job_id, RunStatus.FAILED, error_type="RuntimeError", error_hash="a" * 64)
    await queue.ack(delivery)
    return message


# submit


async def test_submit_creates_the_job_and_queues_the_input(db, queue):
    client = JobClient(db, queue)
    job_id = await client.submit(Shout, In(text="hello"))

    row = await client.status(job_id)
    assert (row.name, row.status, row.resumable, row.resumed_from) == ("shout", RunStatus.INIT, False, None)
    assert (row.job_hash, row.input_hash) == (Shout.job_hash(), input_hash(In(text="hello")))
    delivery = await queue.receive(1.0)
    assert delivery is not None
    assert delivery.message == JobMessage(job_id=job_id, job_name="shout", input={"text": "hello"})
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued"]


async def test_submit_takes_a_job_id_and_the_resumable_flag(db, queue):
    job_id = await JobClient(db, queue).submit(Shout, In(text="a"), resumable=True, job_id="chosen")
    row = await db.get_job(job_id)
    assert job_id == "chosen" and row is not None and row.resumable is True


async def test_an_input_that_is_not_the_jobs_model_is_refused_and_nothing_is_created(db, queue):
    client = JobClient(db, queue)
    with pytest.raises(TypeError, match="shout expects a In"):
        await client.submit(Shout, EchoInput(text="a"))
    assert await queue.receive(0.05) is None
    async with db.conn.execute("SELECT COUNT(*) AS n FROM jobs") as cur:
        row = await cur.fetchone()
        assert row is not None and row["n"] == 0  # no job row either


async def test_a_queue_that_refuses_the_message_fails_the_job_and_raises(db):
    class Broken(MemoryQueue):
        async def enqueue(self, message: JobMessage) -> None:
            raise ConnectionError("broker down")

    with pytest.raises(ConnectionError, match="broker down"):
        await JobClient(db, Broken()).submit(Shout, In(text="a"), job_id="j")
    row = await db.get_job("j")
    assert row is not None and row.status is RunStatus.FAILED
    assert (row.error_type, row.error_hash) == error_fields(ConnectionError("broker down"))
    assert [e.type for e in await db.list_job_events("j")] == ["queued", "failed"]


async def test_an_agent_can_be_submitted_as_a_job(db, queue):
    job_id = await JobClient(db, queue).submit(Echo, EchoInput(text="a"))
    row = await db.get_job(job_id)
    assert row is not None and row.name == "echo" and row.job_hash == ftw.as_job(Echo).job_hash()
    delivery = await queue.receive(1.0)
    assert delivery is not None and delivery.message.input == {"text": "a"}


async def test_a_streaming_agent_cannot_be_submitted(db, queue):
    class Chat(StreamingAgent):
        name: str = "chat"
        description: str = "x"
        version: str = "1"
        prompt: str = "x"

    with pytest.raises(TypeError, match="streams to a person"):
        await JobClient(db, queue).submit(Chat, EchoInput(text="a"))


# resume


async def test_resume_from_makes_a_new_job_that_points_to_the_old_one(db, queue):
    client = JobClient(db, queue)
    first = await client.submit(Shout, In(text="a"), resumable=True)
    await consume(db, queue, finish=RunStatus.FAILED)
    second = await client.submit(Shout, In(text="a"), resume_from=first)
    row = await client.status(second)
    assert row.resumed_from == first and row.resumable is True  # a job that resumes another is resumable too
    delivery = await queue.receive(1.0)
    assert delivery is not None and delivery.message.job_id == second


async def test_resuming_a_job_that_kept_nothing_is_refused_before_anything_is_queued(db, queue):
    client = JobClient(db, queue)
    first = await client.submit(Shout, In(text="a"))  # not resumable
    await consume(db, queue, finish=RunStatus.FAILED)
    with pytest.raises(ValueError, match="did not keep its results"):
        await client.submit(Shout, In(text="a"), resume_from=first)
    assert await queue.receive(0.05) is None
    with pytest.raises(LookupError):
        await client.submit(Shout, In(text="a"), resume_from="nope")


# status


async def test_status_of_an_unknown_job_is_a_lookup_error(db, queue):
    with pytest.raises(LookupError, match="nope"):
        await JobClient(db, queue).status("nope")


# cancel


async def test_cancelling_a_queued_job_marks_it_and_leaves_the_message_for_the_worker_to_skip(db, queue):
    client = JobClient(db, queue)
    job_id = await client.submit(Shout, In(text="a"))
    assert await client.cancel(job_id) is True
    assert (await client.status(job_id)).status is RunStatus.CANCELLED
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "cancelled"]
    delivery = await queue.receive(1.0)  # the message is still there: the worker acknowledges and skips it
    assert delivery is not None and delivery.message.job_id == job_id


async def test_cancelling_a_running_job_sets_the_flag_and_says_so(db, queue):
    client = JobClient(db, queue)
    job_id = await client.submit(Shout, In(text="a"))
    await db.set_job_status(job_id, RunStatus.RUNNING)
    assert await client.cancel(job_id) is True
    row = await client.status(job_id)
    assert row.status is RunStatus.RUNNING and row.cancel_requested is True  # the worker does the cancelling
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "cancel_requested"]
    assert await client.cancel(job_id) is True  # asking twice is fine, and writes no second event
    assert [e.type for e in await db.list_job_events(job_id)].count("cancel_requested") == 1


@pytest.mark.parametrize("final", [RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_cancelling_a_job_that_is_final_changes_nothing(db, queue, final):
    client = JobClient(db, queue)
    job_id = await client.submit(Shout, In(text="a"))
    await db.set_job_status(job_id, final)
    events = await db.list_job_events(job_id)
    assert await client.cancel(job_id) is False
    assert (await client.status(job_id)).status is final and await db.list_job_events(job_id) == events


async def test_cancelling_an_unknown_job_is_false(db, queue):
    assert await JobClient(db, queue).cancel("nope") is False


async def test_a_job_that_started_while_it_was_being_cancelled_is_asked_to_stop(db, queue, monkeypatch):
    client = JobClient(db, queue)
    job_id = await client.submit(Shout, In(text="a"))
    real = db.set_job_status

    async def a_worker_gets_in_first(job_id, status, **kwargs):
        await real(job_id, status, **kwargs)
        if status is RunStatus.CANCELLED:
            await real(job_id, RunStatus.RUNNING)  # the worker's start lands right after ours

    monkeypatch.setattr(db, "set_job_status", a_worker_gets_in_first)
    assert await client.cancel(job_id) is True
    assert (await client.status(job_id)).cancel_requested is True


# result


async def test_the_result_is_read_from_the_users_store(db, queue):
    results = MemoryResultStore()
    client = JobClient(db, queue, results)
    job_id = await client.submit(Shout, In(text="hello"))
    assert await client.result(Shout, job_id) is None  # not done
    await consume(db, queue, results)
    assert await client.result(Shout, job_id) == Out(shouted="HELLO")


async def test_the_result_is_none_when_the_job_failed_or_the_store_has_nothing(db, queue):
    results = MemoryResultStore()
    client = JobClient(db, queue, results)
    failed = await client.submit(Shout, In(text="a"))
    await consume(db, queue, results, finish=RunStatus.FAILED)
    assert await client.result(Shout, failed) is None
    lost = await client.submit(Shout, In(text="b"))
    await consume(db, queue, None)  # succeeded, but nobody kept the result
    assert (await client.status(lost)).status is RunStatus.SUCCESS and await client.result(Shout, lost) is None


async def test_the_result_is_none_without_a_store_and_an_unknown_job_is_an_error(db, queue):
    client = JobClient(db, queue, None)
    job_id = await client.submit(Shout, In(text="a"))
    await consume(db, queue, MemoryResultStore())
    assert client.results is None and await client.result(Shout, job_id) is None
    with pytest.raises(LookupError):
        await client.result(Shout, "nope")


async def test_the_default_store_of_a_sqlite_database_is_the_sqlite_store(db, queue):
    client = JobClient(db, queue)
    assert isinstance(client.results, SqliteResultStore)
    job_id = await client.submit(Shout, In(text="hi"))
    await consume(db, queue, client.results)
    assert await client.result(Shout, job_id) == Out(shouted="HI")


async def test_an_agent_job_result_is_parsed_into_the_agents_output_model(db):
    queue, results = MemoryQueue(), MemoryResultStore()
    client = JobClient(db, queue, results)
    job_id = await client.submit(Echo, EchoInput(text="a"))
    row = await db.get_job(job_id)
    assert row is not None and row.job_hash and row.input_hash
    await results.put(ResultKey(job_id=job_id, agent_hash=row.job_hash, input_hash=row.input_hash), {"echoed": "a"})
    await db.set_job_status(job_id, RunStatus.SUCCESS)
    assert await client.result(Echo, job_id) == EchoOutput(echoed="a")


# events and wait


async def test_events_and_wait_follow_the_job_through_the_client(db, queue):
    client = JobClient(db, queue)
    job_id = await client.submit(Shout, In(text="a"))
    consumer = asyncio.create_task(consume(db, queue, MemoryResultStore()))
    row = await client.wait(job_id, timeout=5)
    await consumer
    assert row.status is RunStatus.SUCCESS
    assert [e.type async for e in client.events(job_id)] == ["queued", "running", "succeeded"]
    assert [e.type async for e in client.events(job_id, after=1)] == ["running", "succeeded"]  # resumes after a seq


async def test_wait_times_out_for_a_job_nobody_runs(db, queue):
    client = JobClient(db, queue)
    job_id = await client.submit(Shout, In(text="a"))
    with pytest.raises(TimeoutError):
        await client.wait(job_id, timeout=0.2)


# a client and a worker in two processes


async def test_a_client_and_a_consumer_share_one_sqlite_file_through_two_database_objects(tmp_path):
    path = tmp_path / "shared.db"
    async with SqliteDatabase(path) as client_db, SqliteDatabase(path) as worker_db:
        client = JobClient(client_db, SqliteQueue(client_db))
        job_id = await client.submit(Shout, In(text="across"))
        consumer = asyncio.create_task(consume(worker_db, SqliteQueue(worker_db), SqliteResultStore(worker_db)))
        row = await client.wait(job_id, timeout=10)  # the events cross over by the one reader of the client's process
        await consumer
        assert row.status is RunStatus.SUCCESS
        assert await client.result(Shout, job_id) == Out(shouted="ACROSS")
        assert await JobClient(worker_db, SqliteQueue(worker_db)).cancel(job_id) is False


def test_the_client_is_on_the_package():
    assert ftw.JobClient is JobClient
