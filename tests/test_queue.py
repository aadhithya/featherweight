"""The job queue: the contract against both queues, and what is particular to sqlite. No model is called."""

import pytest
from pydantic import ValidationError

from featherweight.queue import Delivery, JobMessage, JobQueue, SqliteQueue
from featherweight.sqlite import SqliteDatabase
from featherweight.testing import MemoryQueue, check_queue


def message(n: int) -> JobMessage:
    return JobMessage(job_id=f"job-{n}", job_name="shout", input={"n": n})


async def test_the_sqlite_queue_follows_the_contract(db):
    async def fresh() -> SqliteQueue:
        await db.conn.execute("DELETE FROM job_queue")
        await db.conn.commit()
        return SqliteQueue(db)

    await check_queue(fresh)


async def test_the_memory_queue_follows_the_contract():
    await check_queue(MemoryQueue)


async def test_the_contract_catches_a_queue_that_breaks_it():
    class Greedy(MemoryQueue):
        async def receive(self, timeout):  # hands the same message to everyone
            delivery = await super().receive(timeout)
            if delivery is not None:
                self._ready.insert(0, (int(delivery.token), delivery.message))
            return delivery

    with pytest.raises(AssertionError, match="invisible to other receivers"):
        await check_queue(Greedy)


def test_a_queue_that_misses_a_method_cannot_be_built():
    class Half(JobQueue):
        async def enqueue(self, message): ...

    with pytest.raises(TypeError, match="abstract"):
        Half()  # ty: ignore[abstract-class-instantiation]


def test_messages_and_deliveries_are_frozen_models():
    msg = message(1)
    with pytest.raises(ValidationError):
        msg.job_id = "other"
    assert Delivery(message=msg, token="1").token == "1"
    with pytest.raises(ValidationError):
        JobMessage(job_id="j", job_name="n", input="not a dict")


# what is particular to sqlite


async def test_the_messages_survive_a_reopen_of_the_file(tmp_path):
    async with SqliteDatabase(tmp_path / "q.db") as db:
        queue = SqliteQueue(db)
        await queue.enqueue(message(1))
        await queue.enqueue(message(2))
    async with SqliteDatabase(tmp_path / "q.db") as db:
        queue = SqliteQueue(db)
        first = await queue.receive(0.5)
        assert first is not None and first.message == message(1)
        await queue.ack(first)
        second = await queue.receive(0.5)
        assert second is not None and second.message == message(2)


async def test_a_claimed_message_stays_claimed_when_its_receiver_goes_away(tmp_path):
    """The documented limit: a receiver that dies leaves its message claimed (leases come later)."""
    async with SqliteDatabase(tmp_path / "q.db") as db:
        await SqliteQueue(db).enqueue(message(1))
        claimed = await SqliteQueue(db).receive(0.5)
        assert claimed is not None
    async with SqliteDatabase(tmp_path / "q.db") as db:
        queue = SqliteQueue(db)
        assert await queue.receive(0.05) is None  # nobody else is given it
        await queue.nack(claimed, requeue=True)  # the owner of the token can put it back
        again = await queue.receive(0.5)
        assert again is not None and again.message == message(1)


async def test_a_nacked_message_keeps_its_place_across_a_reconnect(tmp_path):
    async with SqliteDatabase(tmp_path / "q.db") as db:
        queue = SqliteQueue(db)
        for n in (1, 2, 3):
            await queue.enqueue(message(n))
        first = await queue.receive(0.5)
        assert first is not None
        await queue.nack(first, requeue=True)
    async with SqliteDatabase(tmp_path / "q.db") as db:
        queue = SqliteQueue(db)
        ids = []
        while (delivery := await queue.receive(0.05)) is not None:
            ids.append(delivery.message.job_id)
            await queue.ack(delivery)
    assert ids == ["job-1", "job-2", "job-3"]


async def test_the_table_only_accepts_ready_and_claimed(db):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await db.conn.execute(
            "INSERT INTO job_queue (job_id, job_name, input, state, enqueued_at) VALUES ('j', 'n', '{}', 'bogus', 'now')"
        )


async def test_receive_waits_for_a_message_that_arrives_later(db):
    import asyncio

    queue = SqliteQueue(db)

    async def later():
        await asyncio.sleep(0.1)
        await queue.enqueue(message(7))

    task = asyncio.create_task(later())
    delivery = await queue.receive(2.0)
    await task
    assert delivery is not None and delivery.message.job_id == "job-7"
