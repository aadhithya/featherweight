"""The timeout of a job: one execution may run for `Job.timeout` seconds, in a worker and in `run_job` alike."""

import asyncio

import pytest
from fakes import Echo, EchoInput, Reactive, Scripted, runner_with, structured_reply
from pydantic import BaseModel

from featherweight import JobWorker, job, run_job
from featherweight.events import wait_for_job
from featherweight.fingerprint import input_hash
from featherweight.jobs import DEFAULT_JOB_TIMEOUT, Job, JobContext, as_job
from featherweight.models import Status
from featherweight.queue import JobMessage
from featherweight.testing import MemoryQueue, RecordingObserver


class In(BaseModel):
    text: str


class Out(BaseModel):
    shouted: str


class Hangs(Job):
    name = "hangs"
    version = "1"
    input_model = In
    output_model = Out
    timeout = 0.05

    async def run(self, ctx: JobContext, input: In) -> Out:
        await asyncio.sleep(30)
        return Out(shouted=input.text)


class Quick(Job):
    name = "quick"
    version = "1"
    input_model = In
    output_model = Out
    timeout = 5

    async def run(self, ctx: JobContext, input: In) -> Out:
        return Out(shouted=input.text.upper())


class Unlimited(Job):
    name = "unlimited"
    version = "1"
    input_model = In
    output_model = Out
    timeout = None

    async def run(self, ctx: JobContext, input: In) -> Out:
        await asyncio.sleep(0.15)
        return Out(shouted=input.text.upper())


class RaisesItsOwnTimeout(Job):
    name = "own_timeout"
    version = "1"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        await asyncio.wait_for(asyncio.sleep(30), timeout=0.01)  # the job's own TimeoutError, long before the limit
        return Out(shouted="never")


class HangsInAnAgent(Job):
    name = "hangs_in_an_agent"
    version = "1"
    input_model = In
    output_model = Out
    timeout = 0.2

    async def run(self, ctx: JobContext, input: In) -> Out:
        await ctx.run(Echo, EchoInput(text=input.text))  # the agent's model call never returns
        return Out(shouted="never")


def runner(db, *observers):
    return runner_with(
        db, Scripted(script=[structured_reply("EchoOutput", {"echoed": "x"})]), observers=list(observers)
    )


# the declaration


def test_the_default_timeout_is_ten_minutes():
    assert DEFAULT_JOB_TIMEOUT == 600.0

    class Plain(Job):
        name = "plain"
        version = "1"
        input_model = In
        output_model = Out

        async def run(self, ctx, input):
            return Out(shouted="")

    assert Plain.timeout == 600.0


def test_the_decorator_takes_a_timeout():
    @job(timeout=12.5)
    async def slow(ctx: JobContext, input: In) -> Out:
        return Out(shouted="")

    @job
    async def plain(ctx: JobContext, input: In) -> Out:
        return Out(shouted="")

    @job(timeout=None)
    async def forever(ctx: JobContext, input: In) -> Out:
        return Out(shouted="")

    assert (slow.timeout, plain.timeout, forever.timeout) == (12.5, 600.0, None)


def test_an_agent_used_as_a_job_gets_the_default_timeout():
    assert as_job(Echo).timeout == 600.0


@pytest.mark.parametrize("bad", [0, -1, "10", True, float("nan")])
def test_a_timeout_that_is_not_a_positive_number_is_refused_when_the_class_is_defined(bad):
    with pytest.raises(TypeError, match="timeout"):

        class Bad(Job):
            name = "bad"
            version = "1"
            input_model = In
            output_model = Out
            timeout = bad

            async def run(self, ctx, input):
                return Out(shouted="")

    with pytest.raises(TypeError, match="timeout"):

        @job(timeout=bad)
        async def bad_job(ctx: JobContext, input: In) -> Out:
            return Out(shouted="")


def test_the_timeout_is_not_part_of_the_job_hash():
    class Short(Quick):
        timeout = 1

    assert Short.job_hash() == Quick.job_hash()


# run_job


async def test_a_hung_job_fails_with_job_timeout_in_run_job(db):
    outcome = await run_job(runner(db), Hangs, In(text="hello"))

    assert outcome.status is Status.FAILED and "JobTimeout" in (outcome.error or "")
    row = await db.get_job(outcome.job_id)
    assert row is not None and row.status is Status.FAILED
    assert row.error_type == "JobTimeout" and row.error_hash is not None
    events = await db.list_job_events(outcome.job_id)
    assert [e.type for e in events] == ["queued", "running", "failed"]
    assert events[-1].error_type == "JobTimeout"


async def test_a_job_that_finishes_in_time_is_unaffected(db):
    outcome = await run_job(runner(db), Quick, In(text="hi"))

    assert outcome.status is Status.SUCCESS and outcome.result == Out(shouted="HI")


async def test_no_timeout_never_times_out(db):
    outcome = await run_job(runner(db), Unlimited, In(text="hi"))

    assert outcome.status is Status.SUCCESS and outcome.result == Out(shouted="HI")


async def test_a_timeout_error_that_the_job_raises_itself_is_not_a_job_timeout(db):
    outcome = await run_job(runner(db), RaisesItsOwnTimeout, In(text="hi"))

    row = await db.get_job(outcome.job_id)
    assert outcome.status is Status.FAILED
    assert row is not None and row.error_type == "TimeoutError"


async def test_an_outside_cancellation_is_still_cancelled(db):
    class Slow(Hangs):
        name = "slow"
        timeout = 30

    job_id = await db.create_job("slow", job_hash=Slow.job_hash())
    task = asyncio.create_task(run_job(runner(db), Slow, In(text="hi"), job_id=job_id))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await db.get_job(job_id)
    assert row is not None and row.status is Status.CANCELLED and row.error_type is None


async def test_the_observers_see_job_timeout(db):
    seen = RecordingObserver()

    await run_job(runner(db, seen), Hangs, In(text="hello"))

    end = seen.records("job.finish")[0]
    assert end.status is Status.FAILED and end.error_type == "JobTimeout"


async def test_the_timeout_is_not_retried(db):
    calls = []

    class Counts(Hangs):
        name = "counts"

        async def run(self, ctx, input):
            calls.append(1)
            await asyncio.sleep(30)

    await run_job(runner(db), Counts, In(text="hello"))
    await asyncio.sleep(0.1)

    assert calls == [1]


# a worker


async def worker_runs(db, job: type[Job], text="hello") -> str:
    queue = MemoryQueue()
    job_input = In(text=text)
    job_id = await db.create_job(job.name, job_hash=job.job_hash(), input_hash=input_hash(job_input))
    await queue.enqueue(JobMessage(job_id=job_id, job_name=job.name, input=job_input.model_dump(mode="json")))
    worker = JobWorker(db, queue, runner(db), [job])
    task = asyncio.create_task(worker.run())
    try:
        await wait_for_job(db, job_id, timeout=5)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(task, timeout=5)
    return job_id


async def test_a_hung_job_fails_with_job_timeout_in_a_worker(db):
    job_id = await worker_runs(db, Hangs)

    row = await db.get_job(job_id)
    assert row is not None and row.status is Status.FAILED and row.error_type == "JobTimeout"
    assert [e.type for e in await db.list_job_events(job_id)] == ["queued", "running", "failed"]


async def test_a_job_in_a_worker_that_finishes_in_time_succeeds(db):
    job_id = await worker_runs(db, Quick)

    row = await db.get_job(job_id)
    assert row is not None and row.status is Status.SUCCESS


async def test_the_attempts_that_a_timeout_interrupts_end_cancelled(db):
    hanging = Reactive(respond=lambda text, call: structured_reply("EchoOutput", {"echoed": "x"}), delay_s=30)
    outcome = await run_job(runner_with(db, hanging), HangsInAnAgent, In(text="hello"))

    assert outcome.status is Status.FAILED
    row = await db.get_job(outcome.job_id)
    assert row is not None and row.error_type == "JobTimeout"
    runs = await db.list_attempts(job_id=outcome.job_id)
    assert runs and all(r.status is Status.CANCELLED for r in runs)
