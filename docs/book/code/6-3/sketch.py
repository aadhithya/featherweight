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
