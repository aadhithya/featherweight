"""The identity hash: a resume reuses a step only when the run had the same identity. Fake models only."""

import hashlib
import sqlite3

import pytest
from fakes import Echo, EchoInput, Streams, run_pipeline, runner_with
from pydantic import BaseModel, Field
from test_resume import model, texts
from test_streaming import Chat, say

from featherweight.fingerprint import identity_hash, input_hash
from featherweight.identity import IdentitySpec
from featherweight.migrations import MIGRATIONS
from featherweight.models import Status
from featherweight.sqlite import SqliteDatabase


class Teacher(BaseModel):
    user_id: str
    tenant: str = "uni"
    token: str = Field(default="", exclude=True)  # a value that exists only in the process


A = Teacher(user_id="a")
B = Teacher(user_id="b")


def steps(runner, inputs, identity):
    async def pipeline(job_id):
        for text in inputs:
            result = await runner.run(Echo, EchoInput(text=text), job_id=job_id, identity=identity)
            if result.status == Status.FAILED:
                raise RuntimeError(result.error)

    return pipeline


async def run_as(db, identity, fake=None, inputs="abc", **kwargs):
    runner = runner_with(db, fake or model(), max_attempts=1, identity_type=Teacher | None)
    return await run_pipeline(db, "p", steps(runner, inputs, identity), resumable=True, **kwargs)


# the hash


def test_the_hash_is_that_of_the_canonical_json_of_the_identity_and_none_without_one():
    assert identity_hash(None) is None
    assert identity_hash(A) == input_hash(A) == identity_hash(Teacher(user_id="a"))
    assert identity_hash(A) != identity_hash(B)
    assert identity_hash(A) != identity_hash(Teacher(user_id="a", tenant="other"))


def test_a_field_that_is_excluded_is_not_in_the_hash_so_that_a_process_and_a_worker_agree():
    spec = IdentitySpec(Teacher)
    here = Teacher(user_id="a", token="only here")
    on_a_worker = spec.load(spec.dump(here))  # what a message carries, and the worker rebuilds
    assert on_a_worker.token == ""
    assert identity_hash(here) == identity_hash(on_a_worker) == identity_hash(A)


# resuming


async def test_the_same_identity_resumes_and_reuses_the_steps_that_finished(db):
    failed = await run_as(db, A, model({"b"}))
    assert failed.status == Status.FAILED

    healthy = model()
    outcome = await run_as(db, A, healthy, resume_from=failed.job_id)

    assert outcome.status == Status.SUCCESS
    assert texts(healthy) == ["b", "c"]  # "a" was reused: it never reached the model
    new = await db.list_attempts(job_id=outcome.job_id)
    assert new[0].reused_from_attempt_id is not None


async def test_another_identity_reruns_every_step(db):
    failed = await run_as(db, A, model({"b"}))

    healthy = model()
    outcome = await run_as(db, B, healthy, resume_from=failed.job_id)

    assert outcome.status == Status.SUCCESS
    assert texts(healthy) == ["a", "b", "c"]  # nothing was reused: the permissions may have changed
    new = await db.list_attempts(job_id=outcome.job_id)
    assert [r.reused_from_attempt_id for r in new] == [None, None, None]
    assert {r.identity_hash for r in new} == {identity_hash(B)}


@pytest.mark.parametrize(
    "before,after", [(A, None), (None, A)], ids=["an identity, then none", "none, then an identity"]
)
async def test_an_identity_and_no_identity_never_match(db, before, after):
    failed = await run_as(db, before, model({"b"}))

    healthy = model()
    outcome = await run_as(db, after, healthy, resume_from=failed.job_id)

    assert outcome.status == Status.SUCCESS and texts(healthy) == ["a", "b", "c"]


async def test_no_identity_then_no_identity_still_reuses(db):
    failed = await run_as(db, None, model({"b"}))

    healthy = model()
    outcome = await run_as(db, None, healthy, resume_from=failed.job_id)

    assert outcome.status == Status.SUCCESS and texts(healthy) == ["b", "c"]


async def test_a_value_that_exists_only_in_the_process_does_not_stop_the_reuse(db):
    failed = await run_as(db, Teacher(user_id="a", token="first"), model({"b"}))

    healthy = model()
    outcome = await run_as(db, Teacher(user_id="a", token="second"), healthy, resume_from=failed.job_id)

    assert outcome.status == Status.SUCCESS and texts(healthy) == ["b", "c"]


async def test_a_run_records_the_hash_and_not_the_identity(db):
    distinct = Teacher(user_id="u-distinct-789")
    outcome = await run_as(db, distinct, inputs="a")

    (row,) = await db.list_attempts(job_id=outcome.job_id)
    assert row.identity_hash == identity_hash(distinct)
    assert "u-distinct-789" not in repr(row)  # the identity itself is stored nowhere


async def test_a_streamed_run_records_it_too(db):
    runner = runner_with(db, Streams(turns=[["hi"]]), identity_type=Teacher)

    events = [event async for event in runner.stream(Chat, say("hello"), identity=A)]

    assert events[-1].type.value == "RUN_FINISHED"
    (row,) = await db.list_attempts()
    assert row.identity_hash == identity_hash(A)


# the migration


async def test_a_database_from_before_the_column_is_migrated_and_its_runs_have_no_identity(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr("featherweight.sqlite.MIGRATIONS", MIGRATIONS[:1])  # a database made by the first release
    async with SqliteDatabase(path):
        pass
    monkeypatch.undo()  # now the current migrations
    digest = hashlib.sha256(b"x").hexdigest()
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO agent_runs (agent_run_id, invocation_id, attempt, depth, agent_name, agent_version, agent_hash,
               system_prompt_hash, tools_hash, provider, model_name, reasoning, settings, input_hash, status, created_at)
           VALUES ('old-run', 'inv', 1, 1, 'echo', '1', ?, ?, ?, 'fake', 'fake-model', 'low', '{}', ?, 'SUCCESS', '2026-11-20T00:00:00')""",
        (digest, digest, digest, digest),
    )
    conn.commit()
    assert "identity_hash" not in {r[1] for r in conn.execute("PRAGMA table_info(agent_runs)")}
    conn.close()

    async with SqliteDatabase(path) as db:
        row = await db.get_attempt("old-run")
        assert row is not None and row.identity_hash is None  # a run from before the column had no identity
        async with db.conn.execute("SELECT version, name FROM schema_migrations ORDER BY version") as cur:
            assert (2, "identity_hash") in [tuple(r) for r in await cur.fetchall()]
