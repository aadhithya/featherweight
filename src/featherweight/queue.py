"""The queue that connects a client to a job worker, as an abstract class.

Featherweight passes what the user gives to a job: the input travels in the message (as JSON, or as a reference inside it).
`SqliteQueue` is our only product implementation, for local development; how jobs travel in a deployment (Redis, SQS, a
database) is the user's decision, and `featherweight.testing.check_queue` checks an implementation against the contract.
See docs/specs/submitted-jobs.md.
"""

import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict

from featherweight.db import now
from featherweight.sqlite import SqliteDatabase


class JobMessage(BaseModel):
    """What goes on the queue: which job, which job class, and its input as JSON (or references inside it)."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    job_name: str
    input: dict[str, Any]


class Delivery(BaseModel):
    """A message that was received, and the token that identifies the delivery to its queue (opaque: the queue's own)."""

    model_config = ConfigDict(frozen=True)

    message: JobMessage
    token: str


class JobQueue(ABC):
    """Subclass it and write the four methods; one that misses a method cannot be built.

    The contract (`featherweight.testing.check_queue` runs it against any queue):
    - delivery is at-least-once;
    - a received message is invisible to other receivers until it is acknowledged or nacked;
    - `nack(requeue=True)` makes it available again; `nack(requeue=False)` drops it (a broker may dead-letter it);
    - messages come out in the order they went in, where the implementation can;
    - `receive` returns `None` after the timeout;
    - calls can be concurrent.

    What happens to a message whose receiver dies is the implementation's business. `SqliteQueue` leaves it claimed: a
    documented limit until a later release adds leases.
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


class SqliteQueue(JobQueue):
    """A `job_queue` table in the run log's sqlite file, for local development. A message is claimed atomically; `ack`
    deletes its row; `nack` puts it back in its place or deletes it. The input stays in the table until the message is
    acknowledged, on the same terms as the sqlite result store: sqlite is for local development."""

    POLL_START = 0.01
    POLL_MAX = 0.25

    def __init__(self, db: SqliteDatabase):
        self.db = db

    async def enqueue(self, message: JobMessage) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO job_queue (job_id, job_name, input, state, enqueued_at) VALUES (?, ?, ?, 'ready', ?)",
                (message.job_id, message.job_name, json.dumps(message.input), now().isoformat()),
            )

    async def _claim(self) -> Delivery | None:
        async with self.db.transaction() as conn:
            async with conn.execute(
                """UPDATE job_queue SET state = 'claimed', claimed_at = ?
                   WHERE seq = (SELECT seq FROM job_queue WHERE state = 'ready' ORDER BY seq LIMIT 1)
                   RETURNING seq, job_id, job_name, input""",
                (now().isoformat(),),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        message = JobMessage(job_id=row["job_id"], job_name=row["job_name"], input=json.loads(row["input"]))
        return Delivery(message=message, token=str(row["seq"]))

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

    async def ack(self, delivery: Delivery) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM job_queue WHERE seq = ?", (int(delivery.token),))

    async def nack(self, delivery: Delivery, *, requeue: bool) -> None:
        async with self.db.transaction() as conn:
            if requeue:  # back to ready, and the sequence number keeps its place in the order
                await conn.execute(
                    "UPDATE job_queue SET state = 'ready', claimed_at = NULL WHERE seq = ?", (int(delivery.token),)
                )
            else:
                await conn.execute("DELETE FROM job_queue WHERE seq = ?", (int(delivery.token),))
