"""PostgresDatabase: the same contract as sqlite, with native types, in its own schema. Needs FTW_TEST_POSTGRES_URL (the
compose service `postgres` provides it in Docker); without it the tests that need a server are skipped."""

import asyncio
import hashlib
import json
import os
import sys
import uuid
from typing import Any

import pytest
from fakes import Echo, EchoInput, Reactive, structured_reply
from psycopg.sql import SQL, Identifier

from featherweight.db import AttemptRecord
from featherweight.errors import SetupError
from featherweight.migrations import MIGRATIONS, Migration
from featherweight.models import Status
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
    assert await query(schema, "SELECT version, name FROM schema_migrations ORDER BY version") == [
        {"version": m.version, "name": m.name} for m in MIGRATIONS
    ]
    tables = {
        r["table_name"]
        for r in await query(schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", schema)
    }
    assert tables == {
        "jobs",
        "attempts",
        "job_events",
        "schema_migrations",
    }  # no results table: we ship no Postgres result store
    public = await query(
        "public",
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'attempts'",
    )
    assert public == []  # nothing in the application's own schema


@needs_postgres
async def test_a_second_connect_applies_nothing_and_keeps_the_rows(schema):
    async with PostgresDatabase(URL, schema=schema) as db:
        job = await db.create_job("p")
    async with PostgresDatabase(URL, schema=schema) as db:
        assert await db.get_job(job) is not None
    assert (await query(schema, "SELECT COUNT(*) AS n FROM schema_migrations"))[0]["n"] == len(MIGRATIONS)


@needs_postgres
async def test_processes_that_connect_at_the_same_moment_to_a_fresh_schema_all_connect(schema):
    """An API and several workers started together on an empty database: one makes the schema, the rest wait for it."""

    async def connect() -> bool:
        async with PostgresDatabase(URL, schema=schema):
            return True

    assert await asyncio.gather(*(connect() for _ in range(6))) == [True] * 6
    assert await query(schema, "SELECT version FROM schema_migrations ORDER BY version") == [
        {"version": m.version} for m in MIGRATIONS
    ]  # applied once


@needs_postgres
async def test_a_new_migration_is_applied_once_and_a_failing_one_is_rolled_back(schema, monkeypatch):
    async with PostgresDatabase(URL, schema=schema) as db:
        job = await db.create_job("p")
    last = MIGRATIONS[-1].version
    good = Migration(
        version=last + 1, name="second", sql={"sqlite": "", "postgres": "CREATE TABLE second (x integer);"}
    )
    monkeypatch.setattr("featherweight.postgres.MIGRATIONS", [*MIGRATIONS, good])
    async with PostgresDatabase(URL, schema=schema) as db:
        assert await db.get_job(job) is not None
    assert [r["version"] for r in await query(schema, "SELECT version FROM schema_migrations ORDER BY version")] == [
        *[m.version for m in MIGRATIONS],
        last + 1,
    ]

    bad = Migration(
        version=last + 2,
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
    ] == [*[m.version for m in MIGRATIONS], last + 1]


@needs_postgres
async def test_a_schema_from_before_migrations_is_refused_and_left_alone(schema):
    import psycopg

    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute(SQL("CREATE SCHEMA {}").format(Identifier(schema)))
        await conn.execute(SQL("CREATE TABLE {}.attempts (attempt_id text)").format(Identifier(schema)))
    with pytest.raises(SetupError, match="before migrations"):
        await PostgresDatabase(URL, schema=schema).connect()
    tables = {
        r["table_name"]
        for r in await query(schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", schema)
    }
    assert tables == {"attempts"}


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
    assert types[("attempts", "created_at")] == "timestamp with time zone"
    assert types[("attempts", "settings")] == "jsonb"
    assert types[("attempts", "seq")] == "bigint"
    assert types[("attempts", "latency_ms")] == "double precision"
    assert types[("jobs", "resumable")] == "boolean"
    assert types[("jobs", "completed_at")] == "timestamp with time zone"
    run_columns = {column for (table, column) in types if table == "attempts"}
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
        await db.create_attempt(
            AttemptRecord(
                attempt_id="r",
                run_id="i",
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
            await db.conn.execute("UPDATE attempts SET input_tokens = -1")


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
        assert result.status is Status.SUCCESS
        retry = await db.create_job("p", resume_from=job)
        again = await runner.run(Echo, EchoInput(text="secret question"), job_id=retry)
        assert again.reused and sum(fake.counts.values()) == 1  # the second run called no model
        (first, second) = await db.list_attempts()
        assert first.output_hash == second.output_hash and second.reused_from_attempt_id == first.attempt_id

    dump = json.dumps(
        [await query(schema, f"SELECT * FROM {table}") for table in ("jobs", "attempts", "schema_migrations")],
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


@needs_postgres
async def test_a_database_from_before_the_attempts_rename_keeps_its_rows_under_the_new_names(schema, monkeypatch):
    import psycopg

    monkeypatch.setattr("featherweight.postgres.MIGRATIONS", MIGRATIONS[:3])  # the release before the rename
    async with PostgresDatabase(URL, schema=schema):
        pass
    monkeypatch.undo()
    digest = sha("x")
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute(SQL("SET search_path TO {}").format(Identifier(schema)))
        await conn.execute(
            """INSERT INTO agent_runs (agent_run_id, invocation_id, attempt, depth, agent_name, agent_version, agent_hash,
                   system_prompt_hash, tools_hash, provider, model_name, reasoning, settings, input_hash, status, created_at)
               VALUES ('old-attempt', 'old-run', 2, 1, 'echo', '1', %s, %s, %s, 'fake', 'fake-model', 'low', '{}', %s,
                   'SUCCESS', now())""",
            (digest, digest, digest, digest),
        )
        columns = "(agent_run_id, invocation_id, attempt, depth, agent_name, agent_version, agent_hash, system_prompt_hash, tools_hash, provider, model_name, reasoning, settings, input_hash, status, created_at"
        rest = "'echo', '1', %s, %s, %s, 'fake', 'fake-model', 'low', '{}', %s, 'SUCCESS', now()"
        await conn.execute(  # a launched agent: its attempt has a parent
            f"INSERT INTO agent_runs {columns}, parent_agent_run_id) VALUES ('old-child', 'old-run-2', 1, 2, {rest}, 'old-attempt')",
            (digest, digest, digest, digest),
        )
        await conn.execute(  # a step that a resume answered: its attempt names the one it reused
            f"INSERT INTO agent_runs {columns}, reused_from_agent_run_id) VALUES ('old-reused', 'old-run-3', 1, 1, {rest}, 'old-attempt')",
            (digest, digest, digest, digest),
        )
        await conn.execute(
            "INSERT INTO jobs (job_id, name, status, created_at, parent_agent_run_id) VALUES ('old-job', 'bg', 'INIT', now(), 'old-attempt')"
        )
    async with PostgresDatabase(URL, schema=schema) as db:
        row = await db.get_attempt("old-attempt")
        child, reused, job = (
            await db.get_attempt("old-child"),
            await db.get_attempt("old-reused"),
            await db.get_job("old-job"),
        )
    assert row is not None and row.run_id == "old-run" and row.attempt == 2
    assert child is not None and child.parent_attempt_id == "old-attempt" and child.depth == 2
    assert reused is not None and reused.reused_from_attempt_id == "old-attempt"
    assert job is not None and job.parent_attempt_id == "old-attempt"  # a job that an attempt started
    tables = {
        r["table_name"]
        for r in await query(schema, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", schema)
    }
    assert "attempts" in tables and "agent_runs" not in tables


@needs_postgres
async def test_the_constraints_and_the_indexes_of_attempts_do_not_carry_the_retired_table_name(schema):
    async with PostgresDatabase(URL, schema=schema):
        pass
    names = [
        r["name"]
        for r in await query(
            schema,
            "SELECT conname AS name FROM pg_constraint WHERE connamespace = %s::regnamespace "
            "UNION SELECT indexname FROM pg_indexes WHERE schemaname = %s "
            "UNION SELECT sequencename FROM pg_sequences WHERE schemaname = %s",
            schema,
            schema,
            schema,
        )
    ]
    assert names and not [n for n in names if "agent_run" in n], names
    assert {"attempts_pkey", "attempts_status_check", "attempts_job_id_fkey"} <= set(names)
