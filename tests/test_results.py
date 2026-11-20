"""The result store: the contract, the sqlite default, and what is kept for resuming a job. Fake models only."""

import json
import logging

import pytest
from fakes import Echo, EchoInput, Streams, run_pipeline, runner_with
from pydantic import ValidationError
from test_resume import model, run_steps, texts
from test_streaming import Chat, say

from featherweight.errors import SetupError
from featherweight.fingerprint import agent_hash, input_hash
from featherweight.models import RunStatus
from featherweight.results import ResultKey, ResultStore, SqliteResultStore
from featherweight.sqlite import SqliteDatabase
from featherweight.testing import MemoryResultStore, check_result_store

HASH = "a" * 64


def echo_key(job_id: str, text: str = "a") -> ResultKey:
    return ResultKey(job_id=job_id, agent_hash=agent_hash(Echo()), input_hash=input_hash(EchoInput(text=text)))


async def kept(db: SqliteDatabase) -> list[tuple[str, str]]:
    async with db.conn.execute("SELECT job_id, output FROM results ORDER BY job_id") as cur:
        return [(r["job_id"], r["output"]) for r in await cur.fetchall()]


# the contract


async def test_the_sqlite_store_follows_the_contract(db):
    await check_result_store(lambda: SqliteResultStore(db))


async def test_the_memory_store_follows_the_contract():
    await check_result_store(MemoryResultStore)


async def test_the_contract_catches_a_store_that_breaks_it():
    class Forgetful(MemoryResultStore):
        async def delete_job(self, job_id: str) -> None:  # forgets to delete
            return None

    with pytest.raises(AssertionError, match="delete_job must remove every result"):
        await check_result_store(Forgetful)


async def test_the_sqlite_store_keeps_results_in_the_file(tmp_path):
    key = echo_key("job-1")
    async with SqliteDatabase(tmp_path / "f.db") as db:
        await SqliteResultStore(db).put(key, {"echoed": "a"})
    async with SqliteDatabase(tmp_path / "f.db") as db:
        assert await SqliteResultStore(db).get(key) == {"echoed": "a"}


def test_a_store_that_misses_a_method_cannot_be_built():
    class Half(ResultStore):
        async def put(self, key, output): ...

    with pytest.raises(TypeError, match="abstract"):
        Half()  # ty: ignore[abstract-class-instantiation]


def test_a_key_is_a_frozen_model_that_checks_its_hashes():
    key = ResultKey(job_id="j", agent_hash=HASH, input_hash=HASH)
    assert hash(key) == hash(ResultKey(job_id="j", agent_hash=HASH, input_hash=HASH))
    with pytest.raises(ValidationError):
        ResultKey(job_id="j", agent_hash="not a hash", input_hash=HASH)
    with pytest.raises(ValidationError):
        key.job_id = "other"


# a job opts in


async def test_a_job_is_not_resumable_unless_it_says_so(db):
    assert (await db.get_job(await db.create_job("p"))).resumable is False
    assert (await db.get_job(await db.create_job("p", resumable=True))).resumable is True


async def test_a_job_that_resumes_another_is_resumable_itself(db):
    old = await db.create_job("p", resumable=True)
    new = await db.create_job("p", resume_from=old)
    assert (await db.get_job(new)).resumable is True


async def test_resuming_a_job_that_kept_nothing_is_refused_before_any_model_call(db):
    old = await db.create_job("p")
    counting = model()
    with pytest.raises(ValueError, match="did not keep its results"):
        await run_steps(db, "a", counting, resume_from=old)
    assert counting.counts == {}


async def test_job_id_cannot_be_combined_with_resumable(db):
    job = await db.create_job("p")

    async def pipeline(job_id):
        return None

    with pytest.raises(ValueError, match="job_id"):
        await run_pipeline(db, "p", pipeline, job_id=job, resumable=True)


# what is kept, and when


async def test_a_step_of_a_resumable_job_is_kept_under_its_key(db):
    outcome = await run_steps(db, "a")
    store = SqliteResultStore(db)
    assert await store.get(echo_key(outcome.job_id)) == {"echoed": "a", "open_questions": []}


async def test_nothing_is_kept_for_a_job_that_is_not_resumable(db):
    await run_steps(db, "a", resumable=False)
    assert await kept(db) == []


async def test_nothing_is_kept_for_a_run_with_no_job_or_a_failure(db):
    runner = runner_with(db, model(broken={"bad"}), max_attempts=1)
    assert (await runner.run(Echo, EchoInput(text="a"))).status is RunStatus.SUCCESS
    job = await db.create_job("p", resumable=True)
    assert (await runner.run(Echo, EchoInput(text="bad"), job_id=job)).status is RunStatus.FAILED
    assert await kept(db) == []


async def test_nothing_is_kept_for_a_launched_agent_or_a_streaming_run(db):
    from fakes import Scripted
    from test_resume import parent_script
    from test_subagents import Parent, ParentInput

    job = await db.create_job("p", resumable=True)
    runner = runner_with(db, Scripted(script=parent_script("x")), max_attempts=1)
    await runner.run(Parent, ParentInput(task="go"), job_id=job)
    assert [row[0] for row in await kept(db)] == [job]  # the parent only, not its child
    assert len(await kept(db)) == 1

    streaming = runner_with(db, Streams(turns=[["hi"]]))
    async for _ in streaming.stream(Chat, say("hello"), job_id=job):
        pass
    assert len(await kept(db)) == 1  # the streamed turn kept nothing


async def test_a_runner_with_no_store_runs_normally_and_keeps_nothing(db):
    runner = runner_with(db, model(), max_attempts=1, results=None)
    job = await db.create_job("p", resumable=True)
    assert (await runner.run(Echo, EchoInput(text="a"), job_id=job)).status is RunStatus.SUCCESS
    assert await kept(db) == []


# resuming through the store


async def test_a_reused_step_copies_its_result_to_the_new_job(db):
    first = await run_steps(db, "a")
    second = await run_steps(db, "a", resume_from=first.job_id)
    store = SqliteResultStore(db)
    assert await store.get(echo_key(second.job_id)) == await store.get(echo_key(first.job_id))


async def test_a_chain_of_attempts_works_even_when_the_first_jobs_results_are_deleted(db):
    broken = model(broken={"b"})
    first = await run_steps(db, ["a", "b"], broken)
    assert first.status is RunStatus.FAILED
    second = await run_steps(db, ["a", "b"], model(broken={"b"}), resume_from=first.job_id)  # a reused, b fails again
    assert second.status is RunStatus.FAILED

    await SqliteResultStore(db).delete_job(first.job_id)  # the oldest job's results are gone
    ((job_id, text),) = await kept(db)
    assert job_id == second.job_id and json.loads(text) == {"echoed": "a", "open_questions": []}

    counting = model()
    third = await run_steps(db, ["a", "b"], counting, resume_from=second.job_id)
    assert third.status is RunStatus.SUCCESS and texts(counting) == ["b"]  # only b ran
    rows = await db.list_runs(job_id=third.job_id)
    assert [bool(r.reused_from_agent_run_id) for r in rows] == [True, False]


async def test_a_step_the_log_says_succeeded_but_the_store_lost_runs_again(db, caplog):
    first = await run_steps(db, "a")
    await SqliteResultStore(db).delete_job(first.job_id)
    counting = model()
    with caplog.at_level(logging.WARNING, logger="featherweight"):
        outcome = await run_steps(db, "a", counting, resume_from=first.job_id)
    assert outcome.status is RunStatus.SUCCESS and texts(counting) == ["a"]
    assert "result_missing" in caplog.text
    (row,) = await db.list_runs(job_id=outcome.job_id)
    assert row.reused_from_agent_run_id is None


async def test_a_store_that_fails_to_keep_does_not_fail_the_run(db, caplog):
    class Failing(MemoryResultStore):
        async def put(self, key, output):
            raise OSError("disk full")

    runner = runner_with(db, model(), max_attempts=1, results=Failing())
    job = await db.create_job("p", resumable=True)
    with caplog.at_level(logging.WARNING, logger="featherweight"):
        result = await runner.run(Echo, EchoInput(text="a"), job_id=job)
    assert result.status is RunStatus.SUCCESS
    assert "result_not_kept" in caplog.text and "disk full" in caplog.text


async def test_resuming_with_no_store_is_a_setup_error(db):
    first = await run_steps(db, "a")
    new = await db.create_job("p", resume_from=first.job_id)
    runner = runner_with(db, model(), max_attempts=1, results=None)
    with pytest.raises(SetupError, match="no result store"):
        await runner.run(Echo, EchoInput(text="a"), job_id=new)


async def test_a_store_of_the_users_own_serves_a_resume(db):
    store = MemoryResultStore()
    runner = runner_with(db, model(), max_attempts=1, results=store)
    first = await run_pipeline(
        db, "p", lambda job_id: runner.run(Echo, EchoInput(text="a"), job_id=job_id), resumable=True
    )
    assert await store.get(echo_key(first.job_id)) == {"echoed": "a", "open_questions": []}
    counting = model()
    runner = runner_with(db, counting, max_attempts=1, results=store)
    second = await run_pipeline(
        db, "p", lambda job_id: runner.run(Echo, EchoInput(text="a"), job_id=job_id), resume_from=first.job_id
    )
    assert second.result is not None and second.result.reused and counting.counts == {}
    assert await kept(db) == []  # nothing went to the sqlite table: the user's store was used
