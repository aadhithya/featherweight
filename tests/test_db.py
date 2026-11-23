"""The run log in sqlite: the contract of `Database`, and what is particular to a file."""

import asyncio

import pytest
from pydantic import ValidationError

from featherweight.db import AttemptFinish, AttemptRecord, default_db_path, new_id
from featherweight.models import Status
from featherweight.sqlite import SqliteDatabase
from featherweight.testing import check_database

HASH = "a" * 64


def start(**changes) -> AttemptRecord:
    fields = dict(
        attempt_id=new_id(),
        run_id="inv-1",
        attempt=1,
        agent_name="echo",
        agent_version="0.1.0",
        agent_hash=HASH,
        system_prompt_hash=HASH,
        tools_hash=HASH,
        provider="fake",
        model_name="fake-model",
        reasoning="medium",
        settings={"model": "fake-model"},
        input_hash=HASH,
    )
    return AttemptRecord(**{**fields, **changes})


async def test_the_sqlite_database_follows_the_contract(tmp_path):
    await check_database(lambda: SqliteDatabase(tmp_path / "contract.db"))


async def test_data_survives_reopen(tmp_path):
    path = tmp_path / "persist.db"
    async with SqliteDatabase(path) as db1:
        job_id = await db1.create_job("learn")
        run_id = await db1.create_attempt(start(job_id=job_id))
    async with SqliteDatabase(path) as db2:
        job = await db2.get_job(job_id)
        run = await db2.get_attempt(run_id)
        assert job is not None and job.name == "learn"
        assert run is not None and run.agent_name == "echo"


async def test_connect_makes_the_folder_of_the_file(tmp_path):
    path = tmp_path / "a" / "b" / "log.db"
    async with SqliteDatabase(path):
        assert path.exists()


async def test_a_database_that_is_not_connected_says_so(tmp_path):
    db = SqliteDatabase(tmp_path / "x.db")
    with pytest.raises(RuntimeError, match="connect"):
        _ = db.conn
    await db.close()  # closing one that never opened is harmless


async def test_a_closed_database_can_be_opened_again(tmp_path):
    db = SqliteDatabase(tmp_path / "x.db")
    await db.connect()
    job_id = await db.create_job("p")
    await db.close()
    await db.connect()
    assert await db.get_job(job_id) is not None
    await db.close()


def test_the_default_path_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "from-env.db"))
    assert default_db_path() == tmp_path / "from-env.db"
    monkeypatch.delenv("FTW_DB_PATH")
    assert default_db_path().name == "featherweight.db"


def test_ids_are_unique():
    assert len({new_id() for _ in range(1000)}) == 1000


async def test_foreign_keys_are_on(db):
    async with db.conn.execute("PRAGMA foreign_keys") as cur:
        assert (await cur.fetchone())[0] == 1
    with pytest.raises(Exception, match="FOREIGN KEY"):
        await db.create_attempt(start(job_id="missing"))


@pytest.mark.parametrize("status", [Status.INIT, Status.RUNNING])
def test_a_run_ends_in_a_terminal_status(status):
    with pytest.raises(ValidationError, match="SUCCESS, FAILED or CANCELLED"):
        AttemptFinish(status=status)


async def test_a_job_that_resumes_another_is_resumable_and_points_to_it(db):
    old = await db.create_job("p", resumable=True)
    new = await db.create_job("p", resume_from=old)
    job = await db.get_job(new)
    assert job is not None and job.resumed_from == old and job.resumable is True


async def test_a_write_that_fails_does_not_roll_back_the_writes_in_flight_beside_it(tmp_path):
    """Everything shares one connection: without a lock per transaction, a failure rolls back another task's pending work."""
    from featherweight.testing import _run

    async with SqliteDatabase(tmp_path / "x.db") as db:
        job = await db.create_job("p")

        async def fails() -> None:
            with pytest.raises(Exception, match="FOREIGN KEY"):
                await db.create_attempt(_run("no-such-job", "bad"))

        await asyncio.gather(*(fails() for _ in range(10)), db.set_job_status(job, Status.RUNNING))
        row = await db.get_job(job)
        assert row is not None and row.status is Status.RUNNING
        assert [e.type for e in await db.list_job_events(job)] == ["queued", "running"]
