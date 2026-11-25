"""The log lines that docs/specs/units.md names, with the keys that it gives them. People alert on these lines (the migration guide lists them),
so a rename or a lost key must fail a test. Fake models only."""

import asyncio
import logging
import re

import pytest
from fakes import runner_with
from test_resume import Strict, model, run_steps
from test_streaming import Chat, say
from test_takeover import Steps, lost_and_taken_over

from featherweight.models import Status
from featherweight.testing import MemoryQueue


def lines(caplog, name: str) -> list[dict[str, str]]:
    """The lines that begin with `name`, as key=value dictionaries (the text after the keys is left out)."""
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if message.split(" ", 1)[0] == name:
            found.append(dict(re.findall(r"(\w+)=(\S+)", message)))
    return found


def one(caplog, name: str) -> dict[str, str]:
    found = lines(caplog, name)
    assert len(found) == 1, f"{name}: {len(found)} lines"
    return found[0]


async def test_a_run_and_its_attempts_say_run_id_and_attempt_id_where_the_spec_says(db, caplog):
    with caplog.at_level(logging.INFO):
        outcome = await run_steps(db, "a")
    assert outcome.status == Status.SUCCESS
    (row,) = await db.list_attempts(job_id=outcome.job_id)

    started, finished = one(caplog, "run_started"), one(caplog, "run_finished")
    assert started["run_id"] == finished["run_id"] == row.run_id
    attempt = one(caplog, "attempt_started")
    assert (attempt["attempt_id"], attempt["run_id"]) == (row.attempt_id, row.run_id)
    created = one(caplog, "attempt_created")
    assert (created["attempt_id"], created["run_id"], created["job_id"]) == (row.attempt_id, row.run_id, outcome.job_id)
    assert (
        one(caplog, "attempt_running")["attempt_id"] == one(caplog, "attempt_finished")["attempt_id"] == row.attempt_id
    )
    assert one(caplog, "job_started")["job_id"] == outcome.job_id


async def test_no_line_carries_a_retired_key(db, caplog):
    with caplog.at_level(logging.INFO):
        await run_steps(db, "ab")
        run = runner_with(db, model()).stream(Chat, say("hi"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent_run_id=" not in text and "invocation_id=" not in text and "reused_from=" not in text
    assert not re.search(r"\b(run_created|run_running|invocation_started|invocation_finished)\b", text)
    del run


async def test_a_streamed_run_says_both_ids_when_it_starts_and_when_it_ends(db, caplog):
    from fakes import Streams

    runner = runner_with(db, Streams(turns=[["ok"]]))
    with caplog.at_level(logging.INFO):
        run = runner.stream(Chat, say("hi"))
        _ = [event async for event in run]

    for name in ("stream_started", "stream_ended"):
        line = one(caplog, name)
        assert (line["run_id"], line["attempt_id"]) == (run.run_id, run.attempt_id), name


async def test_a_reused_step_says_its_run_its_attempt_and_the_attempt_it_took_the_result_from(db, caplog):
    first = await run_steps(db, "a")
    with caplog.at_level(logging.INFO):
        second = await run_steps(db, "a", resume_from=first.job_id)
    (old,) = await db.list_attempts(job_id=first.job_id)
    (new,) = await db.list_attempts(job_id=second.job_id)

    line = one(caplog, "run_reused")
    assert (line["run_id"], line["attempt_id"], line["reused_from_attempt_id"]) == (
        new.run_id,
        new.attempt_id,
        old.attempt_id,
    )
    assert new.run_id != old.run_id  # a reused step is a run of its own


async def test_a_result_that_is_rejected_says_which_attempt_it_looked_at(db, caplog, monkeypatch):
    first = await run_steps(db, "a", agent=Strict)
    (old,) = await db.list_attempts(job_id=first.job_id)
    monkeypatch.setattr(Strict, "reject", {"a"})  # what check_output allowed, it now rejects
    with caplog.at_level(logging.INFO):
        second = await run_steps(db, "a", model(echoed="fresh"), Strict, resume_from=first.job_id)
    (new,) = await db.list_attempts(job_id=second.job_id)

    line = one(caplog, "run_reuse_rejected")
    assert (line["reused_from_attempt_id"], line["run_id"]) == (old.attempt_id, new.run_id)


async def test_a_result_that_was_not_kept_says_which_attempt_it_looked_at(db, caplog):
    first = await run_steps(db, "a")
    (old,) = await db.list_attempts(job_id=first.job_id)
    await db.conn.execute("DELETE FROM results")  # the log says the step succeeded, and the store has lost its result
    await db.conn.commit()
    with caplog.at_level(logging.INFO):
        second = await run_steps(db, "a", resume_from=first.job_id)
    (new,) = await db.list_attempts(job_id=second.job_id)

    line = one(caplog, "result_missing")
    assert (line["reused_from_attempt_id"], line["run_id"]) == (old.attempt_id, new.run_id)


async def test_a_takeover_says_the_job_and_its_new_execution(db, caplog):
    Steps.started = 0
    Steps.reached, Steps.hold = asyncio.Event(), asyncio.Event()
    with caplog.at_level(logging.INFO):
        job_id, _ = await lost_and_taken_over(db, MemoryQueue(lease=0.3), model())

    line = one(caplog, "worker_takeover")
    assert (line["job_id"], line["execution"]) == (job_id, "2")


@pytest.mark.parametrize("name", ["attempt_created", "attempt_running", "attempt_finished"])
async def test_the_database_logs_each_row_it_makes_once(db, caplog, name):
    with caplog.at_level(logging.INFO):
        await run_steps(db, "a")
    assert len(lines(caplog, name)) == 1
