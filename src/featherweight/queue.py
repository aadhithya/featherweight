"""The queue that connects a client to a job worker, as an abstract class.

featherweight passes what the user gives to a job: the input travels in the message (as JSON, or as a reference inside it).
`SqliteQueue` is our only product implementation, for local development; how jobs travel in a deployment (Redis, SQS, a
database) is the user's decision, and `featherweight.testing.check_queue` checks an implementation against the contract.
See docs/specs/submitted-jobs.md.
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from featherweight.db import now
from featherweight.fingerprint import text_hash
from featherweight.sqlite import SqliteDatabase

logger = logging.getLogger(__name__)

DEAD_LETTERED = "DeadLettered"  # the error type of a job whose message was dead-lettered


class JobMessage(BaseModel):
    """What goes on the queue: which job, which job class, and its input as JSON (or references inside it)."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    job_name: str
    input: dict[str, Any]
    trace_context: dict[
        str, str
    ] = {}  # the submitter's trace context (W3C `traceparent`, ...) if an observer gave one; stored nowhere else


class Delivery(BaseModel):
    """A message that was received, and the token that identifies the delivery to its queue (opaque: the queue's own)."""

    model_config = ConfigDict(frozen=True)

    message: JobMessage
    token: str
    delivery_count: int = 1  # how many times this message has been delivered, this delivery included


class JobQueue(ABC):
    """Subclass it and write the five methods; one that misses a method cannot be built.

    The contract (`featherweight.testing.check_queue` runs it against any queue):
    - delivery is at-least-once;
    - a received message is invisible to other receivers for a **lease**: until it is acknowledged or nacked, or its lease
      ends. If the lease ends first (the receiver died, or stalled), the message becomes available again, and its next
      delivery has a higher `delivery_count`;
    - `extend` renews the lease of a received message. A stale delivery (one whose message was delivered again since) has no
      effect: not on the message, and not on the lease of the new delivery;
    - `nack(requeue=True)` makes it available again; `nack(requeue=False)` drops it (a broker may dead-letter it);
    - messages come out in the order they went in, where the implementation can;
    - `receive` returns `None` after the timeout;
    - calls can be concurrent.

    These are what real brokers already have: SQS's visibility timeout (`ChangeMessageVisibility` is `extend`) and
    `ApproximateReceiveCount` (the delivery count), or a Redis stream's pending list (`XAUTOCLAIM`, and its delivery counter).
    How many redeliveries are allowed is the queue's business: a broker has its own policy and dead-letter queue.
    """

    @abstractmethod
    async def enqueue(self, message: JobMessage) -> None:
        """Put a message on the queue."""

    @abstractmethod
    async def receive(self, timeout: float) -> Delivery | None:
        """The next message, claimed for this receiver, or None when there is none within `timeout` seconds."""

    @abstractmethod
    async def ack(self, delivery: Delivery) -> None:
        """The message was handled: remove it."""

    @abstractmethod
    async def nack(self, delivery: Delivery, *, requeue: bool) -> None:
        """The message was not handled: put it back (`requeue=True`) or drop it (`requeue=False`)."""

    @abstractmethod
    async def extend(self, delivery: Delivery) -> None:
        """Renew the lease of a received message, so that it is not delivered again while its receiver is alive."""


class SqliteQueue(JobQueue):
    """A `job_queue` table in the run log's sqlite file, for local development. A message is claimed atomically for `lease`
    seconds (60 by default); if its receiver does not `ack`, `nack` or `extend` it in time, it is delivered again, with a higher
    `delivery_count`. One that has been delivered `max_deliveries` times (3 by default) and whose lease ends again is marked
    `dead`: it stays in the table, and is not delivered. `ack` deletes the row; `nack` puts it back in its place or deletes it.
    A delivery's token names its claim, so that a stale delivery cannot touch the message after it was delivered again. The
    input stays in the table until the message is acknowledged, on the same terms as the sqlite result store: sqlite is for
    local development."""

    POLL_START = 0.01
    POLL_MAX = 0.25

    def __init__(self, db: SqliteDatabase, *, lease: float = 60.0, max_deliveries: int = 3):
        if lease <= 0:
            raise ValueError("lease must be positive")
        if max_deliveries < 1:
            raise ValueError("max_deliveries must be at least 1")
        self.db = db
        self.lease = lease
        self.max_deliveries = max_deliveries

    async def enqueue(self, message: JobMessage) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO job_queue (job_id, job_name, input, trace_context, state, enqueued_at) VALUES (?, ?, ?, ?, 'ready', ?)",
                (
                    message.job_id,
                    message.job_name,
                    json.dumps(message.input),
                    json.dumps(message.trace_context),
                    _stamp(now()),
                ),
            )

    async def _claim(self) -> Delivery | None:
        dead_jobs: list[str] = []
        async with self.db.transaction() as conn:
            moment = now()
            # A message whose lease ended after its last allowed delivery is dead: it is not delivered, and stays for inspection.
            async with conn.execute(
                """UPDATE job_queue SET state = 'dead', lease_expires_at = NULL
                   WHERE state = 'claimed' AND lease_expires_at <= ? AND delivery_count >= ?
                   RETURNING job_id""",
                (_stamp(moment), self.max_deliveries),
            ) as cur:
                for dead in await cur.fetchall():
                    dead_jobs.append(dead["job_id"])
                    logger.warning(
                        "queue_message_dead job_id=%s max_deliveries=%d", dead["job_id"], self.max_deliveries
                    )
            async with conn.execute(
                """UPDATE job_queue SET state = 'claimed', claimed_at = ?, lease_expires_at = ?, delivery_count = delivery_count + 1
                   WHERE seq = (SELECT seq FROM job_queue
                                WHERE state = 'ready' OR (state = 'claimed' AND lease_expires_at <= ?)
                                ORDER BY seq LIMIT 1)
                   RETURNING seq, job_id, job_name, input, trace_context, delivery_count""",
                (_stamp(moment), _stamp(moment + timedelta(seconds=self.lease)), _stamp(moment)),
            ) as cur:
                row = await cur.fetchone()
        for (
            job_id
        ) in dead_jobs:  # its job will never run: end it, once the table is free (the database lock is not reentrant)
            await self.db.fail_queued_job(job_id, DEAD_LETTERED, text_hash(DEAD_LETTERED))
        if row is None:
            return None
        message = JobMessage(
            job_id=row["job_id"],
            job_name=row["job_name"],
            input=json.loads(row["input"]),
            trace_context=json.loads(row["trace_context"]),
        )
        return Delivery(
            message=message, token=f"{row['seq']}:{row['delivery_count']}", delivery_count=row["delivery_count"]
        )

    async def receive(self, timeout: float) -> Delivery | None:
        deadline = time.monotonic() + timeout
        delay = self.POLL_START
        while True:
            delivery = await self._claim()
            if delivery is not None:
                return delivery
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, self.POLL_MAX)

    @staticmethod
    def _claim_of(delivery: Delivery) -> tuple[int, int]:
        seq, count = delivery.token.split(":")
        return int(seq), int(count)

    async def ack(self, delivery: Delivery) -> None:
        seq, count = self._claim_of(delivery)
        async with self.db.transaction() as conn:
            await conn.execute(
                "DELETE FROM job_queue WHERE seq = ? AND state = 'claimed' AND delivery_count = ?", (seq, count)
            )

    async def nack(self, delivery: Delivery, *, requeue: bool) -> None:
        seq, count = self._claim_of(delivery)
        async with self.db.transaction() as conn:
            if requeue:  # back to ready, and the sequence number keeps its place in the order
                await conn.execute(
                    "UPDATE job_queue SET state = 'ready', claimed_at = NULL, lease_expires_at = NULL "
                    "WHERE seq = ? AND state = 'claimed' AND delivery_count = ?",
                    (seq, count),
                )
            else:
                await conn.execute(
                    "DELETE FROM job_queue WHERE seq = ? AND state = 'claimed' AND delivery_count = ?", (seq, count)
                )

    async def extend(self, delivery: Delivery) -> None:
        seq, count = self._claim_of(delivery)
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE job_queue SET lease_expires_at = ? WHERE seq = ? AND state = 'claimed' AND delivery_count = ?",
                (_stamp(now() + timedelta(seconds=self.lease)), seq, count),
            )


def _stamp(moment: datetime) -> str:
    """A moment as the text that the table keeps: UTC, always with microseconds, so that the text sorts like the time."""
    return moment.isoformat(timespec="microseconds")
