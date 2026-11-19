"""PostgresDatabase: the same contract as sqlite, with native types, in its own schema. Needs FTW_TEST_POSTGRES_URL (the
compose service `postgres` provides it in Docker); without it the tests that need a server are skipped."""

import hashlib
import json
import os
import sys
import uuid
from typing import Any

import pytest
from fakes import Echo, EchoInput, Reactive, structured_reply
from psycopg.sql import SQL, Identifier

from featherweight.db import RunStart
from featherweight.errors import SetupError
from featherweight.migrations import MIGRATIONS, Migration
from featherweight.models import RunStatus
from featherweight.postgres import PostgresDatabase
from featherweight.runner import AgentRunner
from featherweight.testing import MemoryResultStore, check_database

URL = os.environ.get("FTW_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not URL, reason="needs FTW_TEST_POSTGRES_URL (docker compose up -d postgres)")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
async def schema():
    """A schema of its own for each test, dropped after it."""
    import psycopg

    name = f"ftw_test_{uuid.uuid4().hex[:12]}"
    yield name
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute(SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(name)))


async def query(schema: str, sql: Any, *params: Any):
    import psycopg
    from psycopg.rows import dict_row

    async with await psycopg.AsyncConnection.connect(URL, autocommit=True, row_factory=dict_row) as conn:
        await conn.execute(SQL("SET search_path TO {}").format(Identifier(schema)))
        return await (await conn.execute(sql, params)).fetchall()


# the contract


@needs_postgres
async def test_postgres_follows_the_database_contract(schema):
    await check_database(lambda: PostgresDatabase(URL, schema=schema))


# migrations


@needs_postgres
async def test_connect_makes_the_schema_in_its_own_schema_and_records_the_baseline(schema):
    async with PostgresDatabase(URL, schema=schema):
        pass
    assert await query(schema, "SELECT version, name FROM schema_migrations") == [{"version": 1, "name": "baseline"}]
    tables = {
        r["table_name"]
        for r in await query(schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", schema)
    }
    assert tables == {
        "jobs",
        "agent_runs",
        "job_events",
        "schema_migrations",
    }  # no results table: we ship no Postgres result store
    public = await query(
        "public",
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'agent_runs'",
    )
    assert public == []  # nothing in the application's own schema


@needs_postgres
async def test_a_second_connect_applies_nothing_and_keeps_the_rows(schema):
    async with PostgresDatabase(URL, schema=schema) as db:
        job = await db.create_job("p")
    async with PostgresDatabase(URL, schema=schema) as db:
        assert await db.get_job(job) is not None
    assert (await query(schema, "SELECT COUNT(*) AS n FROM schema_migrations"))[0]["n"] == 1


@needs_postgres
async def test_a_new_migration_is_applied_once_and_a_failing_one_is_rolled_back(schema, monkeypatch):
    async with PostgresDatabase(URL, schema=schema) as db:
        job = await db.create_job("p")
    good = Migration(version=2, name="second", sql={"sqlite": "", "postgres": "CREATE TABLE second (x integer);"})
    monkeypatch.setattr("featherweight.postgres.MIGRATIONS", [*MIGRATIONS, good])
    async with PostgresDatabase(URL, schema=schema) as db:
        assert await db.get_job(job) is not None
    assert [r["version"] for r in await query(schema, "SELECT version FROM schema_migrations ORDER BY version")] == [
        1,
        2,
    ]

    bad = Migration(
        version=3,
        name="bad",
        sql={"sqlite": "", "postgres": "CREATE TABLE half (x integer); SELECT nope FROM nowhere;"},
    )
    monkeypatch.setattr("featherweight.postgres.MIGRATIONS", [*MIGRATIONS, good, bad])
    with pytest.raises(Exception, match="nowhere"):
        await PostgresDatabase(URL, schema=schema).connect()
    tables = {
        r["table_name"]
        for r in await query(schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", schema)
    }
    assert "half" not in tables and [
        r["version"] for r in await query(schema, "SELECT version FROM schema_migrations")
    ] == [1, 2]


@needs_postgres
async def test_a_schema_from_before_migrations_is_refused_and_left_alone(schema):
    import psycopg

    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        await conn.execute(SQL("CREATE TABLE {}.agent_runs (agent_run_id text)").format(Identifier(schema)))
    with pytest.raises(SetupError, match="before migrations"):
        await PostgresDatabase(URL, schema=schema).connect()
    tables = {
        r["table_name"]
        for r in await query(schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", schema)
    }
    assert tables == {"agent_runs"}


# native types, and no data


@needs_postgres
async def test_the_columns_have_native_types_and_there_is_no_content_column(schema):
    async with PostgresDatabase(URL, schema=schema):
        pass
    types = {
        (r["table_name"], r["column_name"]): r["data_type"]
        for r in await query(
            schema,
            "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = %s",
            schema,
        )
    }
    assert types[("agent_runs", "created_at")] == "timestamp with time zone"
    assert types[("agent_runs", "settings")] == "jsonb"
    assert types[("agent_runs", "seq")] == "bigint"
    assert types[("agent_runs", "latency_ms")] == "double precision"
    assert types[("jobs", "resumable")] == "boolean"
    assert types[("jobs", "completed_at")] == "timestamp with time zone"
    run_columns = {column for (table, column) in types if table == "agent_runs"}
    from test_migrations import EXPECTED_RUN_COLUMNS

    assert run_columns == EXPECTED_RUN_COLUMNS


@needs_postgres
async def test_the_database_itself_refuses_what_the_schema_forbids(schema):
    import psycopg

    async with PostgresDatabase(URL, schema=schema) as db:
        with pytest.raises(psycopg.errors.CheckViolation):
            await db.conn.execute(
                "INSERT INTO jobs (job_id, name, status, created_at) VALUES ('j', 'p', 'BOGUS', now())"
            )
        await db.create_run(
            RunStart(
                agent_run_id="r",
                invocation_id="i",
                attempt=1,
                agent_name="a",
                agent_version="1",
                agent_hash=sha("a"),
                system_prompt_hash=sha("p"),
                tools_hash=sha("t"),
                provider="fake",
                model_name="m",
                reasoning="low",
                settings={},
                input_hash=sha("i"),
            )  # fmt: skip
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            await db.conn.execute("UPDATE agent_runs SET input_tokens = -1")


@needs_postgres
async def test_timestamps_come_back_timezone_aware_and_in_order(schema):
    async with PostgresDatabase(URL, schema=schema) as db:
        job = await db.create_job("p")
        row = await db.get_job(job)
        assert row is not None and row.created_at.tzinfo is not None and row.created_at.utcoffset().total_seconds() == 0


# the runner on Postgres


@needs_postgres
async def test_a_runner_on_postgres_runs_resumes_and_keeps_no_content(schema):
    from fakes import FakeSettings, runner_with, use_model  # noqa: F401  (the fake model is registered by runner_with)

    def answer(user_text, call_number):
        return structured_reply("EchoOutput", {"echoed": json.loads(user_text)["text"]})

    store = MemoryResultStore()
    async with PostgresDatabase(URL, schema=schema) as db:
        fake = Reactive(respond=answer, delay_s=0)
        runner = runner_with(db, fake, max_attempts=1, results=store)
        assert runner.results is store
        job = await db.create_job("p", resumable=True)
        result = await runner.run(Echo, EchoInput(text="secret question"), job_id=job)
        assert result.status is RunStatus.SUCCESS
        retry = await db.create_job("p", resume_from=job)
        again = await runner.run(Echo, EchoInput(text="secret question"), job_id=retry)
        assert again.reused and sum(fake.counts.values()) == 1  # the second run called no model
        (first, second) = await db.list_runs()
        assert first.output_hash == second.output_hash and second.reused_from_agent_run_id == first.agent_run_id

    dump = json.dumps(
        [await query(schema, f"SELECT * FROM {table}") for table in ("jobs", "agent_runs", "schema_migrations")],
        default=str,
    )
    assert "secret question" not in dump


@needs_postgres
async def test_postgres_has_no_default_result_store(schema):
    async with PostgresDatabase(URL, schema=schema) as db:
        assert AgentRunner(db).results is None  # the user passes `results=`
        job = await db.create_job("p", resumable=True)
        retry = await db.create_job("p", resume_from=job)
        from fakes import runner_with

        runner = runner_with(
            db, Reactive(respond=lambda *_: structured_reply("EchoOutput", {"echoed": "a"}), delay_s=0), results=None
        )
        with pytest.raises(SetupError, match="no result store"):
            await runner.run(Echo, EchoInput(text="a"), job_id=retry)


# no server needed


def test_a_schema_name_must_be_a_plain_identifier():
    for bad in ("Has-Dash", "1starts", "x; DROP TABLE y", "", "UPPER"):
        with pytest.raises(ValueError, match="schema"):
            PostgresDatabase("postgresql://x", schema=bad)
    assert PostgresDatabase("postgresql://x").schema == "featherweight"


async def test_a_missing_psycopg_says_which_extra_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(SetupError, match=r"featherweight\[postgres\]"):
        await PostgresDatabase("postgresql://x").connect()


def test_it_is_on_the_package_without_importing_psycopg():
    import subprocess
    from pathlib import Path

    code = "import sys, featherweight as ftw; ftw.PostgresDatabase; print('psycopg' in sys.modules)"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env).stdout
    assert out.strip() == "False"
