import asyncio

import pytest
from fakes import Echo, EchoInput, Reactive, run_pipeline, runner_with, structured_reply
from pydantic import BaseModel

from featherweight.client import JobClient
from featherweight.fingerprint import input_hash, text_hash
from featherweight.jobs import Job, JobContext, job, run_job
from featherweight.models import RunStatus
from featherweight.queue import SqliteQueue
from featherweight.results import ResultKey
from featherweight.runner import AgentRunner as Runner
from featherweight.sqlite import SqliteDatabase
from featherweight.testing import MemoryResultStore
from featherweight.worker import JobWorker


async def test_a_successful_pipeline_marks_the_job_success_and_returns_its_result(db):
    seen = []

    async def pipeline(job_id):
        seen.append((job_id, (await db.get_job(job_id)).status))
        return {"answer": 42}

    outcome = await run_pipeline(db, "learn", pipeline)

    assert outcome.status == RunStatus.SUCCESS and outcome.result == {"answer": 42} and outcome.error is None
    job = await db.get_job(outcome.job_id)
    assert (job.name, job.status, job.error_type, job.error_hash) == ("learn", RunStatus.SUCCESS, None, None)
    assert job.completed_at is not None
    assert seen == [(outcome.job_id, RunStatus.RUNNING)]  # RUNNING while the pipeline runs, and it gets the job id


@pytest.mark.parametrize("error", [ValueError("unit 3 failed"), RuntimeError("x" * 5000)], ids=["failing", "truncated"])
async def test_a_failing_pipeline_is_returned_as_a_failed_outcome_and_the_job_keeps_the_error_type_and_hash(db, error):
    async def pipeline(job_id):
        raise error

    outcome = await run_pipeline(db, "learn", pipeline)

    expected = f"{type(error).__name__}: {error}"[:2000]  # the outcome returns the text, truncated
    assert outcome.status == RunStatus.FAILED and outcome.result is None
    assert outcome.error == expected
    job = await db.get_job(outcome.job_id)
    assert job.status == RunStatus.FAILED and job.completed_at is not None
    # the job keeps the class of the error and a hash of its whole message, never the text
    assert (job.error_type, job.error_hash) == (type(error).__name__, text_hash(str(error)))
    assert not hasattr(job, "error")


async def test_an_existing_job_can_be_used_so_the_caller_has_the_id_first(db):
    job_id = await db.create_job("learn")

    async def pipeline(received):
        assert received == job_id
        return "done"

    outcome = await run_pipeline(db, "learn", pipeline, job_id=job_id)

    assert outcome.job_id == job_id and outcome.result == "done"
    assert len(await _jobs(db)) == 1  # no second job was created


async def _jobs(db):
    async with db.conn.execute("SELECT job_id FROM jobs") as cur:
        return await cur.fetchall()


async def test_a_cancelled_job_is_marked_cancelled_and_the_cancellation_is_not_swallowed(db):
    started = asyncio.Event()

    async def pipeline(job_id):
        started.set()
        await asyncio.sleep(60)

    job_id = await db.create_job("learn")
    task = asyncio.create_task(run_pipeline(db, "learn", pipeline, job_id=job_id))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    job = await db.get_job(job_id)
    assert (job.status, job.error_type, job.error_hash) == (RunStatus.CANCELLED, None, None)
    assert job.completed_at is not None and job.started_at is not None


# running a job class: `run_job(runner, job, input)`, the same code that a worker runs a job with


class Text(BaseModel):
    text: str


class Shouted(BaseModel):
    shouted: str


@job(version="3")
async def shout(ctx: JobContext, input: Text) -> Shouted:
    return Shouted(shouted=input.text.upper())


@job
async def explode(ctx: JobContext, input: Text) -> Shouted:
    raise ValueError(f"secret about {input.text}")


async def test_run_job_runs_a_job_class_and_returns_its_typed_output(db):
    runner = Runner(db, results=None)

    outcome = await run_job(runner, shout, Text(text="hi"))

    assert outcome.status == RunStatus.SUCCESS and outcome.result == Shouted(shouted="HI")
    row = await db.get_job(outcome.job_id)
    assert (row.name, row.status, row.job_version) == ("shout", RunStatus.SUCCESS, "3")
    assert (row.job_hash, row.input_hash) == (shout.job_hash(), input_hash(Text(text="hi")))
    assert types(await db.list_job_events(outcome.job_id)) == ["queued", "running", "succeeded"]


def types(events):
    return [event.type for event in events]


async def test_run_job_refuses_an_input_of_the_wrong_type_and_a_plain_function(db):
    runner = Runner(db, results=None)

    with pytest.raises(TypeError, match="expects a Text"):
        await run_job(runner, shout, Shouted(shouted="no"))
    with pytest.raises(TypeError, match="neither a Job nor an agent class"):

        async def function(job_id):
            return None

        await run_job(runner, function, Text(text="x"))
    assert await _jobs(db) == []  # nothing was created


async def test_a_failing_job_is_a_failed_outcome_and_keeps_only_the_error_type_and_hash(db):
    outcome = await run_job(Runner(db, results=None), explode, Text(text="x"))

    assert outcome.status == RunStatus.FAILED and outcome.result is None
    assert outcome.error == "ValueError: secret about x"
    row = await db.get_job(outcome.job_id)
    assert (row.status, row.error_type, row.error_hash) == (RunStatus.FAILED, "ValueError", text_hash("secret about x"))


async def test_a_cancelled_task_marks_the_job_cancelled_and_reraises(db):
    started = asyncio.Event()

    class Hang(Job):
        name = "hang"
        version = "1"
        input_model = Text
        output_model = Shouted

        async def run(self, ctx, input):
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(run_job(Runner(db, results=None), Hang, Text(text="x")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (row,) = [await db.get_job(r["job_id"]) for r in await _jobs(db)]
    assert row.status == RunStatus.CANCELLED


async def test_the_output_goes_to_the_runners_result_store_if_it_has_one_and_nowhere_if_not(db):
    store = MemoryResultStore()
    outcome = await run_job(Runner(db, results=store), shout, Text(text="hi"))
    key = ResultKey(job_id=outcome.job_id, agent_hash=shout.job_hash(), input_hash=input_hash(Text(text="hi")))
    assert await store.get(key) == {"shouted": "HI"}

    kept_nowhere = await run_job(Runner(db, results=None), shout, Text(text="hi"))
    assert kept_nowhere.result == Shouted(shouted="HI")  # the caller still gets it back
    assert await store.get(key.model_copy(update={"job_id": kept_nowhere.job_id})) is None


async def test_a_job_whose_output_cannot_be_kept_fails(db):
    class Broken(MemoryResultStore):
        async def put(self, key, output):
            raise OSError("disk full")

    outcome = await run_job(Runner(db, results=Broken()), shout, Text(text="hi"))

    assert outcome.status == RunStatus.FAILED and "disk full" in (outcome.error or "")


async def test_a_job_that_was_cancelled_before_it_started_is_not_run(db):
    job_id = await db.create_job("shout")
    assert await db.cancel_queued_job(job_id)

    outcome = await run_job(Runner(db, results=None), shout, Text(text="hi"), job_id=job_id)

    assert outcome.status == RunStatus.CANCELLED and outcome.result is None and "did not run" in (outcome.error or "")


async def test_run_job_resumes_a_failed_job_like_submit_does(db):
    class Two(BaseModel):
        crash: bool = False

    model = Reactive(respond=lambda text, n: structured_reply("EchoOutput", {"echoed": text}))
    runner = runner_with(db, model, max_attempts=1)

    @job(name="two")
    async def two(ctx: JobContext, input: Two) -> Two:
        await ctx.run(Echo, EchoInput(text="a"))
        if input.crash:
            raise RuntimeError("died")
        return input

    first = await run_job(runner, two, Two(crash=True), resumable=True)
    calls = sum(model.counts.values())
    second = await run_job(runner, two, Two(), resume_from=first.job_id)

    assert first.status == RunStatus.FAILED and second.status == RunStatus.SUCCESS
    assert sum(model.counts.values()) == calls  # the step was answered from the first job's kept result
    assert (await db.get_job(second.job_id)).resumed_from == first.job_id


async def test_run_job_and_a_worker_leave_the_same_row_hashes_events_and_result(tmp_path):
    """The only difference is who runs the job: one function runs it either way."""
    async with SqliteDatabase(tmp_path / "parity.db") as db:
        runner = Runner(db)  # the default result store: a table in the same file
        in_process = await run_job(runner, shout, Text(text="same"))
        queue = SqliteQueue(db)
        client = JobClient(db, queue)
        job_id = await client.submit(shout, Text(text="same"))
        worker = JobWorker(db, queue, runner, jobs=[shout], receive_timeout=0.05)
        running = asyncio.create_task(worker.run())
        row = await client.wait(job_id, timeout=5)
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)

        mine = await db.get_job(in_process.job_id)
        theirs = await db.get_job(job_id)
        fields = ("name", "status", "resumable", "job_version", "job_hash", "input_hash", "error_type", "error_hash")
        assert [getattr(mine, f) for f in fields] == [getattr(theirs, f) for f in fields]
        assert row.status == RunStatus.SUCCESS
        assert types(await db.list_job_events(in_process.job_id)) == types(await db.list_job_events(job_id))
        assert (
            await client.result(shout, job_id)
            == await client.result(shout, in_process.job_id)
            == Shouted(shouted="SAME")
        )


async def test_a_database_in_memory_runs_a_job_with_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with SqliteDatabase(":memory:") as db:
        outcome = await run_job(Runner(db), shout, Text(text="hi"))  # the default result store lives in it too
        assert outcome.status == RunStatus.SUCCESS and outcome.result == Shouted(shouted="HI")
        assert (await db.get_job(outcome.job_id)).status == RunStatus.SUCCESS
        assert types(await db.list_job_events(outcome.job_id)) == ["queued", "running", "succeeded"]
    assert list(tmp_path.iterdir()) == []  # no file, and no ':memory:' file either
