"""The migrations, the schema, and the promise that no table holds data. Fake models only."""

import hashlib
import sqlite3

import pytest
from fakes import Echo, EchoInput, runner_with
from pydantic import ValidationError
from test_resume import model, run_steps
from test_streaming import Chat, say

from featherweight.db import Database, RunEnd, RunStart
from featherweight.errors import SetupError
from featherweight.migrations import MIGRATIONS, Migration
from featherweight.models import RunStatus
from featherweight.sqlite import SqliteDatabase


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def columns(path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


# the migrations


async def test_connect_makes_the_schema_and_records_the_baseline(tmp_path):
    async with SqliteDatabase(tmp_path / "f.db") as db:
        async with db.conn.execute("SELECT version, name FROM schema_migrations") as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [(1, "baseline")]
    assert {"jobs", "agent_runs", "results", "schema_migrations"} <= {
        row[0]
        for row in sqlite3.connect(tmp_path / "f.db").execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


async def test_a_second_connect_applies_nothing_and_keeps_the_rows(tmp_path):
    async with SqliteDatabase(tmp_path / "f.db") as db:
        job = await db.create_job("p")
    async with SqliteDatabase(tmp_path / "f.db") as db:
        assert (await db.get_job(job)) is not None
        async with db.conn.execute("SELECT COUNT(*) FROM schema_migrations") as cur:
            row = await cur.fetchone()
            assert row is not None and row[0] == 1


async def test_a_new_migration_is_applied_once_in_order_and_the_old_rows_stay(tmp_path, monkeypatch):
    async with SqliteDatabase(tmp_path / "f.db") as db:
        job = await db.create_job("p")
    later = [
        *MIGRATIONS,
        Migration(version=3, name="third", sql={"sqlite": "CREATE TABLE third (x INTEGER);"}),
        Migration(version=2, name="second", sql={"sqlite": "CREATE TABLE second (x INTEGER);"}),
    ]
    monkeypatch.setattr("featherweight.sqlite.MIGRATIONS", later)
    async with SqliteDatabase(tmp_path / "f.db") as db:
        assert await db.get_job(job) is not None
        async with db.conn.execute("SELECT version, name FROM schema_migrations ORDER BY applied_at, version") as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [(1, "baseline"), (2, "second"), (3, "third")]
    async with SqliteDatabase(tmp_path / "f.db") as db:  # and not again
        async with db.conn.execute("SELECT COUNT(*) FROM schema_migrations") as cur:
            row = await cur.fetchone()
            assert row is not None and row[0] == 3


async def test_a_migration_that_fails_is_rolled_back_and_the_database_stays_usable(tmp_path, monkeypatch):
    async with SqliteDatabase(tmp_path / "f.db") as db:
        job = await db.create_job("p")
    bad = Migration(version=2, name="bad", sql={"sqlite": "CREATE TABLE half (x INTEGER); SELECT nope FROM nowhere;"})
    monkeypatch.setattr("featherweight.sqlite.MIGRATIONS", [*MIGRATIONS, bad])
    with pytest.raises(sqlite3.OperationalError):
        await SqliteDatabase(tmp_path / "f.db").connect()
    assert "half" not in {r[0] for r in sqlite3.connect(tmp_path / "f.db").execute("SELECT name FROM sqlite_master")}
    monkeypatch.setattr("featherweight.sqlite.MIGRATIONS", MIGRATIONS)
    async with SqliteDatabase(tmp_path / "f.db") as db:  # the failed one left no trace
        assert await db.get_job(job) is not None


async def test_a_database_from_before_migrations_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE agent_runs (agent_run_id TEXT PRIMARY KEY, input TEXT, output TEXT);")
    conn.commit()
    conn.close()
    with pytest.raises(SetupError, match="before migrations.*Delete it"):
        await SqliteDatabase(path).connect()
    assert "schema_migrations" not in {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}


# the schema holds no data


EXPECTED_RUN_COLUMNS = {
    "seq", "agent_run_id", "invocation_id", "attempt", "job_id", "parent_agent_run_id", "depth", "agent_name",
    "agent_version", "agent_hash", "system_prompt_hash", "tools_hash", "provider", "model_name", "reported_model_name",
    "reasoning", "settings", "input_hash", "output_hash", "reused_from_agent_run_id", "thread_id", "status", "error_type",
    "error_hash", "input_tokens", "output_tokens", "latency_ms", "created_at", "started_at", "completed_at",
}  # fmt: skip


async def test_the_tables_have_exactly_the_documented_columns_and_none_of_them_is_data(tmp_path):
    async with SqliteDatabase(tmp_path / "f.db"):
        pass
    runs = set(columns(tmp_path / "f.db", "agent_runs"))
    assert runs == EXPECTED_RUN_COLUMNS
    jobs = set(columns(tmp_path / "f.db", "jobs"))
    assert jobs == {
        "job_id", "name", "status", "resumable", "resumed_from", "job_version", "job_hash", "input_hash", "cancel_requested",
        "worker_id", "lease_expires_at", "executions", "error_type", "error_hash", "created_at", "started_at", "completed_at",
    }  # fmt: skip
    for table_columns in (runs, jobs):
        assert not {"input", "output", "tool_names", "error", "messages"} & table_columns


SECRETS = ("secret question", "secret answer", "secret failure", "secret tool")


async def dump(path) -> str:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return "\n".join(
            repr(row) for table in tables if table != "results" for row in conn.execute(f"SELECT * FROM {table}")
        )
    finally:
        conn.close()


async def test_no_table_but_the_result_store_holds_an_input_an_output_or_an_error_text(tmp_path):
    import json

    from fakes import Reactive, structured_reply

    def answer(user_text, call_number):
        text = json.loads(user_text)["text"]
        if text == "secret failure":
            return RuntimeError("secret failure: the model said secret answer")
        return structured_reply("EchoOutput", {"echoed": "secret answer"})

    path = tmp_path / "f.db"
    async with SqliteDatabase(path) as db:
        runner = runner_with(db, Reactive(respond=answer, delay_s=0), max_attempts=1)
        job = await db.create_job("p", resumable=True)
        await runner.run(Echo, EchoInput(text="secret question"), job_id=job)  # success
        failed = await runner.run(Echo, EchoInput(text="secret failure"), job_id=job)  # failure
        assert "secret failure" in (failed.error or "")  # the caller still gets the text
        retry = await db.create_job("p", resume_from=job)
        reused = await runner.run(Echo, EchoInput(text="secret question"), job_id=retry)  # reused
        assert reused.reused
        streaming = runner_with(db, __import__("fakes").Streams(turns=[["secret answer"]]))
        async for _ in streaming.stream(Chat, say("secret question"), job_id=job):
            pass
        await db.set_job_status(job, RunStatus.FAILED, error_type="RuntimeError", error_hash=sha("secret failure"))

    text = await dump(path)
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} is in the run log"
    assert "RuntimeError" in text  # the type of the error is


# the typed records


def start(**changes) -> RunStart:
    fields = {
        "agent_run_id": "r", "invocation_id": "i", "attempt": 1, "agent_name": "a", "agent_version": "1",
        "agent_hash": sha("a"), "system_prompt_hash": sha("p"), "tools_hash": sha("t"), "provider": "fake",
        "model_name": "m", "reasoning": "low", "settings": {}, "input_hash": sha("i"),
    }  # fmt: skip
    return RunStart(**{**fields, **changes})


@pytest.mark.parametrize(
    "change",
    [
        {"agent_hash": "not a hash"},
        {"tools_hash": sha("t").upper()},
        {"input_hash": "abc"},
        {"attempt": 0},
        {"depth": 0},
        {"unknown_field": 1},
    ],
)
def test_a_run_start_with_a_malformed_field_is_refused(change):
    with pytest.raises(ValidationError):
        start(**change)


def test_a_run_start_is_frozen():
    with pytest.raises(ValidationError):
        start().attempt = 2


@pytest.mark.parametrize(
    "end",
    [
        {"status": RunStatus.RUNNING},
        {"status": RunStatus.INIT},
        {"status": RunStatus.FAILED, "error_type": "ValueError"},  # a type without its hash
        {"status": RunStatus.FAILED, "error_hash": sha("x")},
        {"status": RunStatus.SUCCESS, "input_tokens": -1},
        {"status": RunStatus.SUCCESS, "latency_ms": -0.5},
        {"status": RunStatus.SUCCESS, "output_hash": "nope"},
        {"status": RunStatus.SUCCESS, "output": {"text": "data"}},  # no output in a record
    ],
)
def test_a_run_end_with_a_malformed_field_is_refused(end):
    with pytest.raises(ValidationError):
        RunEnd(**end)


async def test_the_database_itself_refuses_what_the_schema_forbids(tmp_path):
    async with SqliteDatabase(tmp_path / "f.db") as db:
        with pytest.raises(sqlite3.IntegrityError):
            await db.conn.execute(
                "INSERT INTO jobs (job_id, name, status, created_at) VALUES ('j', 'p', 'BOGUS', 'now')"
            )
        await db.create_run(start())
        with pytest.raises(sqlite3.IntegrityError):
            await db.conn.execute("UPDATE agent_runs SET input_tokens = -1")


def test_a_database_that_misses_a_method_cannot_be_built():
    class Half(Database):
        async def connect(self) -> None: ...

    with pytest.raises(TypeError, match="abstract"):
        Half()  # ty: ignore[abstract-class-instantiation]


async def test_a_failed_connect_closes_the_connection(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE agent_runs (x TEXT);")
    conn.close()
    db = SqliteDatabase(path)
    with pytest.raises(SetupError):
        await db.connect()
    with pytest.raises(RuntimeError, match="not connected"):
        db.conn  # noqa: B018


async def test_a_resumed_job_is_still_traced_without_data(tmp_path):
    async with SqliteDatabase(tmp_path / "f.db") as db:
        first = await run_steps(db, "a")
        second = await run_steps(db, "a", model(), resume_from=first.job_id)
        rows = await db.list_runs(job_id=second.job_id)
        assert [bool(r.reused_from_agent_run_id) for r in rows] == [True]
        assert rows[0].output_hash == (await db.list_runs(job_id=first.job_id))[0].output_hash
