"""Following a job: its events, as an async stream, and waiting for it to end.

The database writes an event in the transaction of every status change (see `Database`). Here they are read:
- **in the process that wrote them**, the database publishes each event at commit, so a listener hears it at once, with no
  polling;
- **from another process**, the events table must be read, so each `Database` object has **one reader task** that polls it, with
  a growing delay, for every open stream together (one query, however many streams), and fans the events out.
A stream reads the stored events first and listens second, and drops what it has already given, so it misses nothing and gives
nothing twice. See docs/specs/submitted-jobs.md.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from featherweight.db import FINAL, FINAL_EVENTS, Database, JobEvent, JobRow

logger = logging.getLogger(__name__)

POLL_START = 0.05  # seconds between two reads of the table when something has just arrived...
POLL_MAX = 1.0  # ...growing to this while nothing does


class _Subscription:
    """One open stream: the job it follows, the last event it gave, and the events that have reached it but not been given."""

    def __init__(self, job_id: str, after: int):
        self.job_id = job_id
        self.last = after  # the last seq given to the consumer
        self.queued = after  # the last seq put in `pending`
        self.pending: list[JobEvent] = []
        self.wake = asyncio.Event()


class EventHub:
    """The listeners of one `Database` object, and the one reader task that serves the ones in other processes."""

    def __init__(self, db: Database):
        self._db = db
        self._subs: dict[str, list[_Subscription]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.polls = 0  # how many times the table has been read: a count for the tests

    @property
    def open_streams(self) -> int:
        return sum(len(subs) for subs in self._subs.values())

    def subscribe(self, job_id: str, after: int) -> _Subscription:
        sub = _Subscription(job_id, after)
        self._subs.setdefault(job_id, []).append(sub)
        if self._reader is None:
            self._reader = asyncio.create_task(self._read())
        return sub

    def unsubscribe(self, sub: _Subscription) -> None:
        subs = self._subs.get(sub.job_id, [])
        if sub in subs:
            subs.remove(sub)
        if not subs:
            self._subs.pop(sub.job_id, None)
        if not self._subs and self._reader is not None:
            self._reader.cancel()  # the last stream is closed: the reader goes with it
            self._reader = None

    def publish(self, event: JobEvent) -> int:
        """Give an event to the streams that follow its job and have not had it. Returns how many had it new."""
        fresh = 0
        for sub in self._subs.get(event.job_id, []):
            if event.seq > sub.queued:
                sub.queued = event.seq
                sub.pending.append(event)
                sub.wake.set()
                fresh += 1
        return fresh

    async def _read(self) -> None:
        delay = POLL_START
        while self._subs:
            after = min(sub.last for subs in self._subs.values() for sub in subs)
            try:
                events = await self._db.poll_job_events(list(self._subs), after)
            except Exception as exc:  # a closed or failing database must not kill the streams: they will see it too
                logger.warning("events_poll_failed error_type=%s", type(exc).__name__)
                events = []
            self.polls += 1
            fresh = sum(self.publish(event) for event in events)
            delay = POLL_START if fresh else min(delay * 2, POLL_MAX)
            await asyncio.sleep(delay)


def hub_for(db: Database) -> EventHub:
    """The hub of a database object, made on first use."""
    hub = db.__dict__.get("_event_hub")
    if hub is None:
        hub = db.__dict__["_event_hub"] = EventHub(db)
    return hub


async def job_events(db: Database, job_id: str, after: int = 0) -> AsyncGenerator[JobEvent, None]:
    """The events of a job, after the sequence number `after`: the stored ones first, then each as it happens, ending after
    a final one (`succeeded`, `failed` or `cancelled`). A stream that starts late replays what it missed, and one that
    reconnects passes the `seq` of the last event it had. `LookupError` for a job that does not exist."""
    if await db.get_job(job_id) is None:
        raise LookupError(f"No such job: {job_id}")
    hub = hub_for(db)
    sub = hub.subscribe(job_id, after)  # listen before reading, so that nothing falls in the gap
    try:
        stored = await db.list_job_events(job_id, after=after)
        for event in stored:
            if event.seq > sub.last:
                sub.last = event.seq
                yield event
                if event.type in FINAL_EVENTS:
                    return
        if not stored:
            job = await db.get_job(job_id)
            if (
                job is not None
                and job.status in FINAL
                and not await db.list_job_events(job_id, after=sub.last, limit=1)
            ):
                return  # it ended before `after`: nothing more will come
        while True:
            await sub.wake.wait()
            sub.wake.clear()
            batch, sub.pending = sub.pending, []
            for event in batch:
                if event.seq > sub.last:
                    sub.last = event.seq
                    yield event
                    if event.type in FINAL_EVENTS:
                        return
    finally:
        hub.unsubscribe(sub)


async def wait_for_job(db: Database, job_id: str, timeout: float | None = None) -> JobRow:
    """The job, once it is final (`SUCCESS`, `FAILED` or `CANCELLED`). It reads the status, and if the job is not final it
    follows the events, so there is no gap between looking and listening. `TimeoutError` after `timeout` seconds,
    `LookupError` for a job that does not exist. A convenience for scripts and tests: a web handler should return the job id."""
    job = await db.get_job(job_id)
    if job is None:
        raise LookupError(f"No such job: {job_id}")
    if job.status in FINAL:
        return job

    async def follow() -> None:
        async with contextlib.aclosing(job_events(db, job_id)) as events:
            async for _ in events:
                pass

    await asyncio.wait_for(follow(), timeout)
    final = await db.get_job(job_id)
    assert final is not None
    return final
