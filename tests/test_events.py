"""Job events: written with every status change, streamed from the database, and sent as SSE. Fake models only."""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest

import featherweight as ftw
from featherweight import sse
from featherweight.db import JobEvent
from featherweight.events import hub_for, job_events, wait_for_job
from featherweight.jobs import run_job
from featherweight.models import RunStatus
from featherweight.postgres import PostgresDatabase
from featherweight.sqlite import SqliteDatabase

URL = os.environ.get("FTW_TEST_POSTGRES_URL")


@pytest.fixture(params=["sqlite", "postgres"])
async def any_db(request, tmp_path):
    """A database of each kind (Postgres only when there is a server), opened."""
    if request.param == "sqlite":
        async with SqliteDatabase(tmp_path / "events.db") as db:
            yield db
        return
    if not URL:
        pytest.skip("needs FTW_TEST_POSTGRES_URL (docker compose up -d postgres)")
    import psycopg
    from psycopg.sql import SQL, Identifier

    schema = f"ftw_test_{uuid.uuid4().hex[:12]}"
    async with PostgresDatabase(URL, schema=schema) as db:
        yield db
    async with await psycopg.AsyncConnection.connect(URL, autocommit=True) as conn:
        await conn.execute(SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(schema)))


def types(events: list[JobEvent]) -> list[str]:
    return [e.type for e in events]


async def take(stream, n: int, timeout: float = 2.0) -> list[JobEvent]:
    return [await asyncio.wait_for(anext(stream), timeout) for _ in range(n)]


# every change makes exactly one event


async def test_every_status_change_makes_exactly_one_event_whoever_makes_it(any_db):
    db = any_db
    job = await db.create_job("p")
    await db.set_job_status(job, RunStatus.RUNNING)
    await db.request_job_cancel(job)
    await db.set_job_status(job, RunStatus.CANCELLED)
    failed = await db.create_job("p")
    await db.set_job_status(failed, RunStatus.FAILED, error_type="ValueError", error_hash="a" * 64)

    assert types(await db.list_job_events(job)) == ["queued", "running", "cancel_requested", "cancelled"]
    events = await db.list_job_events(failed)
    assert types(events) == ["queued", "failed"]
    assert (events[1].status, events[1].error_type, events[1].error_hash) == (RunStatus.FAILED, "ValueError", "a" * 64)


async def test_run_job_makes_the_events_of_its_statuses(any_db):
    db = any_db

    async def good(job_id):
        return "ok"

    async def bad(job_id):
        raise RuntimeError("boom")

    async def slow(job_id):
        await asyncio.sleep(60)

    ok = await run_job(db, "p", good)
    no = await run_job(db, "p", bad)
    started = asyncio.Event()

    async def wait_then_run(job_id):
        started.set()
        await slow(job_id)

    job = await db.create_job("p")
    task = asyncio.create_task(run_job(db, "p", wait_then_run, job_id=job))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert types(await db.list_job_events(ok.job_id)) == ["queued", "running", "succeeded"]
    assert types(await db.list_job_events(no.job_id)) == ["queued", "running", "failed"]
    assert types(await db.list_job_events(job)) == ["queued", "running", "cancelled"]


async def test_the_event_and_the_status_are_written_together_or_not_at_all(any_db, monkeypatch):
    db = any_db
    job = await db.create_job("p")

    async def refuse(*args, **kwargs):
        raise RuntimeError("the event cannot be written")

    monkeypatch.setattr(type(db), "_write_event", refuse)
    with pytest.raises(RuntimeError, match="cannot be written"):
        await db.set_job_status(job, RunStatus.RUNNING)
    with pytest.raises(RuntimeError, match="cannot be written"):
        await db.request_job_cancel(job)
    with pytest.raises(RuntimeError, match="cannot be written"):
        await db.create_job("p", job_id="never")
    monkeypatch.undo()

    row = await db.get_job(job)
    assert row is not None and row.status is RunStatus.INIT and row.cancel_requested is False  # neither changed
    assert await db.get_job("never") is None  # nor was the new job written
    assert types(await db.list_job_events(job)) == ["queued"]


# streams


async def test_a_late_listener_replays_what_it_missed_and_the_stream_ends_at_the_final_event(any_db):
    db = any_db
    job = await db.create_job("p")
    await db.set_job_status(job, RunStatus.RUNNING)
    await db.set_job_status(job, RunStatus.SUCCESS)

    events = [e async for e in job_events(db, job)]
    assert types(events) == ["queued", "running", "succeeded"]
    assert [e.seq for e in await _after(db, job, events[0].seq)] == [e.seq for e in events[1:]]  # a reconnect resumes
    assert await _after(db, job, events[-1].seq) == []  # one that has seen the end gets nothing, and does not hang


async def _after(db, job, seq):
    return await asyncio.wait_for(_collect(job_events(db, job, after=seq)), 2)


async def _collect(stream):
    return [e async for e in stream]


async def test_a_listener_that_starts_midway_misses_nothing_and_gets_nothing_twice(any_db):
    db = any_db
    job = await db.create_job("p")
    await db.set_job_status(job, RunStatus.RUNNING)
    stream = job_events(db, job)
    first = await take(stream, 2)  # the stored ones
    assert types(first) == ["queued", "running"]

    await db.set_job_status(job, RunStatus.SUCCESS)
    last = await take(stream, 1)
    assert types(last) == ["succeeded"]
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 2)  # it ended
    seqs = [e.seq for e in [*first, *last]]
    assert seqs == sorted(set(seqs))


async def test_a_listener_in_the_same_process_hears_at_commit_with_no_polling(any_db, monkeypatch):
    db = any_db

    async def nothing(*args, **kwargs):
        return []  # the table is never read: only the publication at commit can reach the stream

    monkeypatch.setattr(type(db), "poll_job_events", nothing)
    job = await db.create_job("p")
    stream = job_events(db, job)
    assert types(await take(stream, 1)) == ["queued"]
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)  # let the stream start to wait
    await db.set_job_status(job, RunStatus.RUNNING)
    assert (await asyncio.wait_for(waiting, 0.5)).type == "running"
    await stream.aclose()


async def test_a_listener_in_another_process_hears_through_the_one_reader(tmp_path):
    path = tmp_path / "two.db"
    async with SqliteDatabase(path) as writer, SqliteDatabase(path) as reader:  # like two processes on one file
        job = await writer.create_job("p")
        stream = job_events(reader, job)
        assert types(await take(stream, 1)) == ["queued"]
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.01)
        await writer.set_job_status(job, RunStatus.RUNNING)  # the reader's own database was not told
        assert (await asyncio.wait_for(waiting, 3)).type == "running"
        await writer.set_job_status(job, RunStatus.SUCCESS)
        assert types(await take(stream, 1)) == ["succeeded"]
        assert hub_for(reader).polls >= 1


async def test_one_reader_serves_many_streams(tmp_path):
    path = tmp_path / "many.db"
    async with SqliteDatabase(path) as writer, SqliteDatabase(path) as reader:
        jobs = [await writer.create_job("p") for _ in range(30)]
        calls = 0
        original = reader.poll_job_events

        async def counting(job_ids, after=0):
            nonlocal calls
            calls += 1
            return await original(job_ids, after)

        reader.poll_job_events = counting  # type: ignore[method-assign]
        streams = [job_events(reader, job) for job in jobs]
        await asyncio.gather(*(take(stream, 1) for stream in streams))  # all open, all idle now
        hub = hub_for(reader)
        assert hub.open_streams == 30
        waiting = [asyncio.create_task(anext(stream)) for stream in streams]
        before = calls
        await asyncio.sleep(0.6)
        assert calls - before < 15, "one reader polls once for every stream, not once per stream"
        for job in jobs:
            await writer.set_job_status(job, RunStatus.RUNNING)
        got = await asyncio.wait_for(asyncio.gather(*waiting), 5)
        assert {e.type for e in got} == {"running"} and {e.job_id for e in got} == set(jobs)
        for stream in streams:
            await stream.aclose()


async def test_the_reader_stops_with_the_last_stream(any_db):
    db = any_db
    job = await db.create_job("p")
    one, two = job_events(db, job), job_events(db, job)
    await take(one, 1)
    await take(two, 1)
    hub = hub_for(db)
    assert hub.open_streams == 2 and hub._reader is not None
    await one.aclose()
    assert hub.open_streams == 1 and hub._reader is not None
    await two.aclose()
    assert hub.open_streams == 0 and hub._reader is None


async def test_a_stream_for_a_job_that_does_not_exist_is_an_error(any_db):
    with pytest.raises(LookupError, match="no-such-job"):
        await anext(job_events(any_db, "no-such-job"))


# wait


async def test_wait_for_job_returns_a_final_job_and_waits_for_one_that_is_not(any_db):
    db = any_db
    job = await db.create_job("p")
    waiting = asyncio.create_task(wait_for_job(db, job, timeout=5))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    await db.set_job_status(job, RunStatus.RUNNING)
    await db.set_job_status(job, RunStatus.FAILED, error_type="X", error_hash="b" * 64)
    row = await asyncio.wait_for(waiting, 5)
    assert row.status is RunStatus.FAILED and row.error_type == "X"
    assert (await wait_for_job(db, job)).status is RunStatus.FAILED  # already final: no waiting


async def test_wait_for_job_times_out_and_refuses_a_job_that_does_not_exist(any_db):
    db = any_db
    job = await db.create_job("p")
    with pytest.raises(TimeoutError):
        await wait_for_job(db, job, timeout=0.1)
    assert hub_for(db).open_streams == 0  # the timed-out wait let go of its stream
    with pytest.raises(LookupError):
        await wait_for_job(db, "no-such-job")


# SSE


def event(seq=12, type="running", status=RunStatus.RUNNING, **fields) -> JobEvent:
    return JobEvent(
        seq=seq, job_id="job-1", type=type, status=status, at=datetime(2026, 11, 20, 10, 0, 1, tzinfo=UTC), **fields
    )


def test_an_event_is_sse_byte_for_byte():
    assert sse.format_event(event()) == (
        "id: 12\nevent: running\n"
        'data: {"seq":12,"job_id":"job-1","type":"running","status":"RUNNING","error_type":null,"error_hash":null,'
        '"at":"2026-11-20T10:00:01Z"}\n\n'
    )
    failed = event(13, "failed", RunStatus.FAILED, error_type="ValueError", error_hash="c" * 64)
    assert '"error_type":"ValueError","error_hash":"' + "c" * 64 + '"' in sse.format_event(failed)


def test_the_headers_are_those_of_a_stream_that_is_not_buffered():
    assert sse.HEADERS == {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def source(items, pause=0.0):
    for item in items:
        if pause:
            await asyncio.sleep(pause)
        yield item


async def test_the_stream_is_encoded_and_ends_when_the_events_end():
    out = [piece async for piece in sse.encode(source([event(1, "queued", RunStatus.INIT), event(2)]))]
    assert [p.split("\n")[0] for p in out] == ["id: 1", "id: 2"]


async def test_a_keep_alive_comment_goes_out_while_nothing_arrives():
    out = [piece async for piece in sse.encode(source([event()], pause=0.25), keep_alive=0.05)]
    assert out[0] == ": keep-alive\n\n" and out[-1].startswith("id: 12") and out.count(": keep-alive\n\n") >= 2


async def test_the_source_is_closed_when_the_consumer_stops():
    closed = asyncio.Event()

    async def slow():
        try:
            while True:
                await asyncio.sleep(0.01)
                yield event()
        finally:
            closed.set()

    stream = sse.encode(slow())
    assert (await anext(stream)).startswith("id: 12")
    await stream.aclose()
    assert closed.is_set()


async def test_a_job_followed_over_sse_resumes_from_last_event_id(any_db):
    db = any_db
    job = await db.create_job("p")
    await db.set_job_status(job, RunStatus.RUNNING)
    await db.set_job_status(job, RunStatus.SUCCESS)
    everything = [p async for p in sse.encode(job_events(db, job))]
    assert [p.split("\n")[1] for p in everything] == ["event: queued", "event: running", "event: succeeded"]
    last_event_id = int(everything[0].split("\n")[0].removeprefix("id: "))  # the browser had the first
    resumed = [p async for p in sse.encode(job_events(db, job, after=last_event_id))]
    assert resumed == everything[1:]


def test_it_is_all_on_the_package():
    assert (
        ftw.sse is sse
        and ftw.JobEvent is JobEvent
        and ftw.job_events is job_events
        and ftw.wait_for_job is wait_for_job
    )
