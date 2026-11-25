"""The job queue: the contract against both queues, and what is particular to sqlite. No model is called."""

import pytest
from fakes import settings
from pydantic import ValidationError

from featherweight.queue import Delivery, JobMessage, JobQueue, SqliteQueue
from featherweight.sqlite import SqliteDatabase
from featherweight.testing import MemoryQueue, check_queue


def sqlite_queue(db, lease: float = 60.0, max_deliveries: int = 3) -> SqliteQueue:
    """A `SqliteQueue` whose settings (`FTW_QUEUE_LEASE`, `FTW_QUEUE_MAX_DELIVERIES`) are these while it is built."""
    with settings(queue_lease=lease, queue_max_deliveries=max_deliveries):
        return SqliteQueue(db)


def message(n: int) -> JobMessage:
    return JobMessage(job_id=f"job-{n}", job_name="shout", input={"n": n})


async def test_the_sqlite_queue_follows_the_contract(db):
    async def fresh(lease: float) -> SqliteQueue:
        await db.conn.execute("DELETE FROM job_queue")
        await db.conn.commit()
        return sqlite_queue(db, lease=lease)

    await check_queue(fresh)


async def test_the_memory_queue_follows_the_contract():
    await check_queue(MemoryQueue)


async def test_the_contract_catches_a_queue_that_breaks_it():
    class Greedy(MemoryQueue):
        async def receive(self, timeout):  # hands the same message to everyone
            delivery = await super().receive(timeout)
            if delivery is not None:
                seq, _ = delivery.token.split(":")
                self._ready.insert(0, (int(seq), delivery.delivery_count - 1, delivery.message))
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
    assert Delivery(message=msg, token="1").delivery_count == 1
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


async def test_a_message_whose_receiver_went_away_is_delivered_again_after_its_lease(tmp_path):
    async with SqliteDatabase(tmp_path / "q.db") as db:
        await SqliteQueue(db).enqueue(message(1))
        claimed = await sqlite_queue(db, lease=0.3).receive(0.5)
        assert claimed is not None and claimed.delivery_count == 1
    async with SqliteDatabase(tmp_path / "q.db") as db:
        queue = sqlite_queue(db, lease=0.3)
        assert await queue.receive(0.05) is None  # nobody else is given it while the lease lasts
        again = await queue.receive(2.0)  # its receiver is gone: the lease ends, and it comes back
        assert again is not None and again.message == message(1) and again.delivery_count == 2
        await queue.ack(claimed)  # the first delivery is stale now, and cannot remove the new claim
        await queue.nack(claimed, requeue=False)
        await queue.extend(claimed)
        rows = [tuple(r) for r in await db.conn.execute_fetchall("SELECT state, delivery_count FROM job_queue")]
        assert rows == [("claimed", 2)]
        await queue.ack(again)
        assert await db.conn.execute_fetchall("SELECT 1 FROM job_queue") == []


async def test_extend_renews_the_lease_in_the_table(db):
    queue = sqlite_queue(db, lease=5)
    await queue.enqueue(message(1))
    delivery = await queue.receive(0.5)
    assert delivery is not None
    before = [tuple(r) for r in await db.conn.execute_fetchall("SELECT lease_expires_at FROM job_queue")][0][0]
    import asyncio

    await asyncio.sleep(0.05)
    await queue.extend(delivery)
    after = [tuple(r) for r in await db.conn.execute_fetchall("SELECT lease_expires_at FROM job_queue")][0][0]
    assert after > before


async def test_a_message_delivered_max_deliveries_times_is_marked_dead_and_not_delivered(db, caplog):
    queue = sqlite_queue(db, lease=0.1, max_deliveries=3)
    await queue.enqueue(message(1))
    counts = []
    for _ in range(3):  # the receiver of job-1 goes quiet every time
        delivery = await queue.receive(2.0)
        assert delivery is not None and delivery.message.job_id == "job-1"
        counts.append(delivery.delivery_count)
    assert counts == [1, 2, 3]
    await queue.enqueue(message(2))
    with caplog.at_level("WARNING"):
        other = await queue.receive(2.0)  # job-1's third lease ends: dead; job-2 is the next one
    assert other is not None and other.message.job_id == "job-2"
    await queue.ack(other)
    assert await queue.receive(0.3) is None
    rows = await db.conn.execute_fetchall("SELECT job_id, state, delivery_count FROM job_queue")
    assert [tuple(r) for r in rows] == [("job-1", "dead", 3)]  # kept for inspection
    assert "queue_message_dead job_id=job-1" in caplog.text


async def test_a_nack_with_requeue_does_not_count_against_max_deliveries(db):
    queue = sqlite_queue(db, lease=5, max_deliveries=1)
    await queue.enqueue(message(1))
    first = await queue.receive(0.5)
    assert first is not None
    await queue.nack(first, requeue=True)  # a graceful shutdown hands it back: that is not a lost delivery
    second = await queue.receive(0.5)
    assert second is not None and second.delivery_count == 2


async def test_the_lease_and_the_limit_are_checked(db):
    with pytest.raises(ValueError, match="FTW_QUEUE_LEASE"):
        sqlite_queue(db, lease=0)
    with pytest.raises(ValueError, match="FTW_QUEUE_MAX_DELIVERIES"):
        sqlite_queue(db, max_deliveries=0)
    with pytest.raises(ValueError, match="lease"):
        MemoryQueue(lease=-1)


async def test_the_memory_queue_delivers_again_after_its_lease_with_a_higher_count():
    queue = MemoryQueue(lease=0.1)
    await queue.enqueue(message(1))
    first = await queue.receive(0.5)
    assert first is not None and first.delivery_count == 1
    again = await queue.receive(2.0)
    assert again is not None and again.delivery_count == 2
    await queue.ack(first)  # stale: no effect
    third = await queue.receive(2.0)
    assert third is not None and third.delivery_count == 3


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


async def test_the_table_only_accepts_ready_claimed_and_dead(db):
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
