# A job that a worker runs

Submit a job to a queue and let a worker run it, in another process. The example puts the worker in the same script so that it runs on its own.

```python
import asyncio

from pydantic import BaseModel

from featherweight import (
    AgentRunner,
    JobClient,
    JobContext,
    JobWorker,
    SqliteDatabase,
    SqliteQueue,
    default_db_path,
    job,
)


class Text(BaseModel):
    text: str


class Shouted(BaseModel):
    shouted: str


@job
async def shout(ctx: JobContext, input: Text) -> Shouted:
    return Shouted(shouted=input.text.upper())


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db:
        queue = SqliteQueue(db)
        worker = JobWorker(db, queue, AgentRunner(db), jobs=[shout])  # a worker runs only the jobs you list
        running = asyncio.create_task(worker.run())  # in real life a process of its own

        client = JobClient(db, queue)  # the client only writes to the queue and the database
        job_id = await client.submit(shout, Text(text="hello"))
        async for event in client.events(job_id):  # until the job is final; or sse.encode(...) from a web handler
            print(event.type)  # queued, running, succeeded
        print(await client.result(shout, job_id))

        await worker.stop()
        await running


if __name__ == "__main__":
    asyncio.run(main())
```

Run as many workers as you like: a worker that dies loses its lease, and another takes the job over (`executions` + 1; a resumable job reuses its finished steps). A job must be safe to run twice, and `@job(timeout=...)` (600 s by default) stops one that hangs. [The submitted jobs guide](../submitted-jobs.md) has cancelling, shutdown, leases and writing your own queue. `examples/jobs` runs it, and `takeover.py` kills a worker mid-job.
