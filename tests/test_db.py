import sqlite3

import pytest
from pydantic import BaseModel

from micro.db import Database, new_id
from micro.models import RunStatus


class Payload(BaseModel):
    text: str


def run_kwargs(**overrides):
    kwargs = dict(
        agent_run_id=new_id(),
        invocation_id="inv-1",
        attempt=1,
        job_id=None,
        agent_name="echo",
        agent_version="0.1.0",
        model_name="gemini-3.5-flash-lite",
        reasoning="medium",
        tool_names=["shout"],
        system_prompt_hash="abc123",
        input=Payload(text="hi"),
    )
    kwargs.update(overrides)
    return kwargs


async def test_create_and_get_job(db):
    job_id = await db.create_job("learn")
    job = await db.get_job(job_id)
    assert job.name == "learn"
    assert job.status == RunStatus.INIT
    assert job.created_at
    assert job.completed_at is None
    assert job.error is None


async def test_job_status_error_and_completed_at(db):
    await db.create_job("generate", job_id="job-1")
    await db.set_job_status("job-1", RunStatus.RUNNING)
    job = await db.get_job("job-1")
    assert job.status == RunStatus.RUNNING and job.completed_at is None

    await db.set_job_status("job-1", RunStatus.FAILED, error="Unit 3 failed: boom")
    job = await db.get_job("job-1")
    assert job.status == RunStatus.FAILED and job.error == "Unit 3 failed: boom"
    assert job.completed_at is not None

    await db.set_job_status("job-1", RunStatus.RUNNING)  # e.g. a resume: the completion time and the error go
    job = await db.get_job("job-1")
    assert job.completed_at is None and job.error is None

    await db.set_job_status("job-1", RunStatus.SUCCESS, error="ignored")  # only FAILED keeps an error
    job = await db.get_job("job-1")
    assert job.status == RunStatus.SUCCESS and job.error is None
    assert job.completed_at and job.completed_at >= job.created_at


async def test_unknown_job_and_run(db):
    assert await db.get_job("nope") is None
    assert await db.get_run("nope") is None
    with pytest.raises(LookupError):
        await db.set_job_status("nope", RunStatus.RUNNING)
    with pytest.raises(LookupError):
        await db.mark_run_running("nope")


async def test_run_lifecycle_success(db):
    job_id = await db.create_job("learn")
    run_id = await db.create_run(**run_kwargs(job_id=job_id))

    row = await db.get_run(run_id)
    assert row.status == RunStatus.INIT
    assert row.created_at and row.completed_at is None
    assert row.output is None and row.error is None
    assert row.input == {"text": "hi"}
    assert row.tool_names == ["shout"]
    assert row.job_id == job_id

    await db.mark_run_running(run_id)
    row = await db.get_run(run_id)
    assert row.status == RunStatus.RUNNING
    assert row.completed_at is None

    await db.finish_run(
        run_id,
        status=RunStatus.SUCCESS,
        output=Payload(text="HI"),
        input_tokens=10,
        output_tokens=20,
        latency_ms=12.5,
        reported_model_name="gemini-3.5-flash-lite",
    )
    row = await db.get_run(run_id)
    assert row.status == RunStatus.SUCCESS
    assert row.output == {"text": "HI"}
    assert (row.input_tokens, row.output_tokens, row.latency_ms) == (10, 20, 12.5)
    assert row.reported_model_name == "gemini-3.5-flash-lite"
    assert row.error is None
    assert row.completed_at and row.completed_at >= row.created_at

    failed_id = await db.create_run(**run_kwargs())  # a failed run keeps its input and has no output
    await db.finish_run(failed_id, status=RunStatus.FAILED, error="boom", latency_ms=3.0)
    row = await db.get_run(failed_id)
    assert (row.status, row.error, row.output) == (RunStatus.FAILED, "boom", None)
    assert row.input == {"text": "hi"} and row.completed_at is not None


@pytest.mark.parametrize("status", [RunStatus.INIT, RunStatus.RUNNING])
async def test_finish_run_requires_terminal_status(db, status):
    run_id = await db.create_run(**run_kwargs())
    with pytest.raises(ValueError):
        await db.finish_run(run_id, status=status)


async def test_run_with_unknown_job_is_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.create_run(**run_kwargs(job_id="missing"))


async def test_attempts_group_by_invocation_and_job(db):
    job_id = await db.create_job("learn")
    first = await db.create_run(**run_kwargs(job_id=job_id, invocation_id="inv-A", attempt=1))
    second = await db.create_run(**run_kwargs(job_id=job_id, invocation_id="inv-A", attempt=2))
    other = await db.create_run(**run_kwargs(job_id=job_id, invocation_id="inv-B", attempt=1))
    standalone = await db.create_run(**run_kwargs(invocation_id="inv-C"))

    attempts = await db.list_runs(invocation_id="inv-A")
    assert [r.agent_run_id for r in attempts] == [first, second]
    assert [r.attempt for r in attempts] == [1, 2]

    by_job = await db.list_runs(job_id=job_id)
    assert [r.agent_run_id for r in by_job] == [first, second, other]
    assert standalone not in [r.agent_run_id for r in by_job]
    assert len(await db.list_runs()) == 4


async def test_data_survives_reopen(tmp_path):
    path = tmp_path / "persist.db"
    async with Database(path) as db1:
        job_id = await db1.create_job("learn")
        run_id = await db1.create_run(**run_kwargs(job_id=job_id))
    async with Database(path) as db2:
        assert (await db2.get_job(job_id)).name == "learn"
        assert (await db2.get_run(run_id)).agent_name == "echo"


OLD_SCHEMA = """
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                   completed_at TEXT, error TEXT);
CREATE TABLE agent_runs (
    agent_run_id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL, attempt INTEGER NOT NULL,
    job_id TEXT REFERENCES jobs(job_id), agent_name TEXT NOT NULL, agent_version TEXT NOT NULL,
    model_name TEXT NOT NULL, reported_model_name TEXT, reasoning TEXT NOT NULL, tool_names TEXT NOT NULL,
    system_prompt_hash TEXT NOT NULL, status TEXT NOT NULL, input TEXT NOT NULL, output TEXT, input_tokens INTEGER,
    output_tokens INTEGER, error TEXT, latency_ms REAL, created_at TEXT NOT NULL, completed_at TEXT
);
INSERT INTO agent_runs VALUES ('old', 'inv', 1, NULL, 'echo', '0.1.0', 'm', NULL, 'low', '[]', 'h', 'SUCCESS',
                               '{}', NULL, NULL, NULL, NULL, NULL, '2026-01-01T00:00:00+00:00', NULL);
"""


async def test_an_older_database_gets_the_new_columns_when_it_is_opened(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    async with Database(path) as db:
        (old,) = await db.list_runs()
        assert old.agent_run_id == "old" and old.parent_agent_run_id is None and old.depth == 1  # the defaults
        assert old.provider is None and old.settings is None  # a row from before there were settings
        assert old.agent_hash is None  # never matched for reuse
        await db.create_run(
            **run_kwargs(parent_agent_run_id="old", depth=2, provider="openai", settings='{"model": "x"}')
        )
        await db.create_run(**run_kwargs(agent_hash="f" * 64))
        assert (await db.list_runs())[-1].agent_hash == "f" * 64
        newest = (await db.list_runs())[1]
        assert (
            newest.agent_hash is None
            and newest.depth == 2
            and newest.provider == "openai"
            and newest.settings == {"model": "x"}
        )

    async with Database(path) as db:  # opening it again changes nothing
        assert len(await db.list_runs()) == 3
