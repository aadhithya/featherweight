import asyncio

import pytest

from featherweight.jobs import run_job
from featherweight.models import RunStatus


async def test_a_successful_pipeline_marks_the_job_success_and_returns_its_result(db):
    seen = []

    async def pipeline(job_id):
        seen.append((job_id, (await db.get_job(job_id)).status))
        return {"answer": 42}

    outcome = await run_job(db, "learn", pipeline)

    assert outcome.status == RunStatus.SUCCESS and outcome.result == {"answer": 42} and outcome.error is None
    job = await db.get_job(outcome.job_id)
    assert (job.name, job.status, job.error) == ("learn", RunStatus.SUCCESS, None)
    assert job.completed_at is not None
    assert seen == [(outcome.job_id, RunStatus.RUNNING)]  # RUNNING while the pipeline runs, and it gets the job id


@pytest.mark.parametrize("error", [ValueError("unit 3 failed"), RuntimeError("x" * 5000)], ids=["failing", "truncated"])
async def test_a_failing_pipeline_is_returned_as_a_failed_outcome_with_the_error_in_the_job(db, error):
    async def pipeline(job_id):
        raise error

    outcome = await run_job(db, "learn", pipeline)

    expected = f"{type(error).__name__}: {error}"[:2000]  # long errors are truncated
    assert outcome.status == RunStatus.FAILED and outcome.result is None
    assert outcome.error == expected
    job = await db.get_job(outcome.job_id)
    assert (job.status, job.error) == (RunStatus.FAILED, expected)
    assert job.completed_at is not None


async def test_an_existing_job_can_be_used_so_the_caller_has_the_id_first(db):
    job_id = await db.create_job("learn")

    async def pipeline(received):
        assert received == job_id
        return "done"

    outcome = await run_job(db, "learn", pipeline, job_id=job_id)

    assert outcome.job_id == job_id and outcome.result == "done"
    assert len(await _jobs(db)) == 1  # no second job was created


async def _jobs(db):
    async with db.conn.execute("SELECT job_id FROM jobs") as cur:
        return await cur.fetchall()


async def test_a_cancelled_job_is_marked_failed_and_the_cancellation_is_not_swallowed(db):
    started = asyncio.Event()

    async def pipeline(job_id):
        started.set()
        await asyncio.sleep(60)

    job_id = await db.create_job("learn")
    task = asyncio.create_task(run_job(db, "learn", pipeline, job_id=job_id))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    job = await db.get_job(job_id)
    assert (job.status, job.error) == (RunStatus.FAILED, "Cancelled")
