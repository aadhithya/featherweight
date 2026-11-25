<!-- Copied from docs/book/06-bring-your-own-storage/03-a-job-queue.md by scripts/build_skill.py. Edit the original, not this file. -->

# A job queue

Write a job queue over the queue you run.

A submitted job travels through a `JobQueue`. The queues are `SqliteQueue` (ours, for one machine), `MemoryQueue` (for tests) and yours, over SQS, a Redis
stream or a table. The interface is five methods: `enqueue`, `receive`, `ack`, `nack` and `extend`.

The one rule that matters is the **lease**: a received message is yours for a while. If you do not `ack`, `nack` or `extend` it in time, it comes back
with a higher `delivery_count`. That is how a message from a dead worker is delivered again. Real brokers already have it:

| | SQS | Redis stream |
|---|---|---|
| `enqueue` | `send_message` | `XADD` |
| `receive` | `receive_message`, with the lease as the visibility timeout | `XREADGROUP` for new messages, `XAUTOCLAIM` for ones idle too long |
| `ack` | `delete_message` | `XACK` |
| `extend` | `change_message_visibility` | `XCLAIM` your own entry, which resets its idle time |
| `delivery_count` | `ApproximateReceiveCount` | the delivery counter of `XPENDING` |

This is **a sketch**. It is not run or tested here, because there is no SQS or Redis in this book or in its tests:

<!-- code: code/6-3/sketch.py -->
```python
"""Chapter 6-3: a sketch of a job queue over SQS. It is not run or tested here: there is no SQS in this book's container.

Queues: SqliteQueue (ours, for one machine), MemoryQueue (for tests), or yours: SQS, a Redis stream, a table.
"""

from featherweight import Delivery, JobMessage, JobQueue


class SqsQueue(JobQueue):
    def __init__(self, client, url: str, lease: int = 60):
        self.client, self.url, self.lease = client, url, lease  # a boto3 SQS client, and the queue's URL

    async def enqueue(self, message: JobMessage) -> None:
        raise NotImplementedError  # send_message(MessageBody=message.model_dump_json())

    async def receive(self, timeout: float) -> Delivery | None:
        raise NotImplementedError  # receive_message(WaitTimeSeconds=..., VisibilityTimeout=self.lease): the lease is the visibility timeout
        # Delivery(message=..., token=<the receipt handle>, delivery_count=<ApproximateReceiveCount>)

    async def ack(self, delivery: Delivery) -> None:
        raise NotImplementedError  # delete_message(ReceiptHandle=delivery.token)

    async def nack(self, delivery: Delivery, *, requeue: bool) -> None:
        raise NotImplementedError  # requeue: change_message_visibility(..., 0); otherwise delete_message, or let the redrive policy dead-letter it

    async def extend(self, delivery: Delivery) -> None:
        raise NotImplementedError  # change_message_visibility(..., self.lease)
```

Check yours with the kit. It gives your factory a short lease, to see a message come back:

```python
await testing.check_queue(lambda lease: SqsQueue(client, url, lease=int(lease)))
```

> **Why** the lease is in the interface: a queue that loses a message when a worker dies would strand its job. See [the queue](https://github.com/aadhithya/featherweight/blob/main/docs/specs/submitted-jobs.md#the-queue) and
> [your own queue](../../submitted-jobs.md#your-own-queue).

**Also possible**

- [A queue that signs its messages](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-queue-that-signs-its-messages.md), as a recipe.
