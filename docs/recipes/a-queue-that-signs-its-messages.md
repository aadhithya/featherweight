# A queue that signs its messages

featherweight trusts its queue: a message says who a run is for, and whoever can write to the queue can write that. Secure the queue the way you secure any broker (access control, TLS, encryption at rest). If you
want a signature of your own too, wrap a queue: `JobQueue` is the seam, and a wrapper is a queue like any other.

```python
import hashlib
import hmac
import json
import logging
import time

from featherweight.queue import Delivery, JobMessage, JobQueue

logger = logging.getLogger(__name__)
SIGNATURE = "x-signature"  # a key of `trace_context`: the one map of strings that every message carries


class HmacQueue(JobQueue):
    """Signs a message when it goes in, and drops one that does not carry the signature of its content when it comes out."""

    def __init__(self, inner: JobQueue, key: bytes):
        self.inner, self.key = inner, key

    def _sign(self, message: JobMessage) -> str:
        trace = {k: v for k, v in message.trace_context.items() if k != SIGNATURE}
        body = message.model_copy(update={"trace_context": trace}).model_dump(mode="json")
        return hmac.new(self.key, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    async def enqueue(self, message: JobMessage) -> None:
        signed = {**message.trace_context, SIGNATURE: self._sign(message)}
        await self.inner.enqueue(message.model_copy(update={"trace_context": signed}))

    async def receive(self, timeout: float) -> Delivery | None:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            delivery = await self.inner.receive(left)
            if delivery is None:
                return None
            message = delivery.message
            plain = message.model_copy(
                update={"trace_context": {k: v for k, v in message.trace_context.items() if k != SIGNATURE}}
            )
            if hmac.compare_digest(message.trace_context.get(SIGNATURE, ""), self._sign(plain)):
                return delivery.model_copy(update={"message": plain})  # as it was put in
            logger.warning("message_dropped job_id=%s: its signature is not valid", message.job_id)
            await self.inner.nack(
                delivery, requeue=False
            )  # the job row is not touched: a forged message may name a real job
        return None

    async def ack(self, delivery: Delivery) -> None:
        await self.inner.ack(delivery)

    async def nack(self, delivery: Delivery, *, requeue: bool) -> None:
        await self.inner.nack(delivery, requeue=requeue)

    async def extend(self, delivery: Delivery) -> None:
        await self.inner.extend(delivery)
```

Check it like any queue, with the kit, and give the client and the worker the wrapper, with the same key:

```python
# yours: db (the database), runner (an AgentRunner) and key (a secret from your vault)
from featherweight import JobClient, JobWorker, testing
from featherweight.queue import SqliteQueue
from featherweight.testing import MemoryQueue

await testing.check_queue(lambda lease: HmacQueue(MemoryQueue(lease=lease), key=b"a secret from your vault"))

queue = HmacQueue(SqliteQueue(db), key)
client, worker = JobClient(db, queue), JobWorker(db, queue, runner, jobs=[...])
```

<!-- skill:omit -->
- **The signature covers the whole message:** the job id and name, the input, the identity and its kind, and the trace context, so an identity cannot be moved onto another job. It is a signature, not encryption: the message stays readable.
- **A forged or changed message is dropped** (`nack(requeue=False)`), and the job row is left alone, because the `job_id` of a forged message can name someone's real job.
- **A replay does no harm.** A valid message that someone puts on the queue again (a copy of one they saw) is for a job that already ran: the worker starts only a job that is queued, so it acknowledges the message and runs nothing. The signature binds the job, the input and the identity, so a replay can at most start that same job, once. If you also want to refuse old messages, put a timestamp in `trace_context` and check it in `receive`.
- **Rotating the key:** accept a list of keys on `receive` and sign with the first.
- **Encryption** is the same shape: encrypt `input` and `identity` in `enqueue` and decrypt them in `receive`, and leave `job_id`, `job_name` and `trace_context` readable, because a broker uses them. featherweight
  ships none (it has no cryptography dependency): bring the library you trust, and run the kit on the result.
- The signature travels in `trace_context` only because every queue already carries that map unchanged, and the kit checks that it does. See [identity](../specs/identity.md#across-the-queue).
<!-- /skill:omit -->
