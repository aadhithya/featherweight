<!-- Copied from docs/book/05-in-production/01-submit-and-a-worker.md by scripts/build_skill.py. Edit the original, not this file. -->

# Submit and a worker

Submit a job from one process and run it on a worker.

Part 5 takes the assistant into production. It builds on the jobs of Part 3 (`digest.py` is the job `summarise_all` from there) and on the chat of Part 4
(chapter 5-3): read those first. Each folder runs on its own.

Until now you ran the job in your own process with `run_job`. Here a client **submits** it, and a **worker** runs it.

<!-- code: code/5-1/app.py -->
```python
"""Chapter 5-1: submit the job, and let a worker run it. They only meet in the queue and the database."""

import asyncio
from pathlib import Path

from digest import Digest, Reports, summarise_all

from featherweight import AgentRunner, JobClient, JobWorker, SqliteDatabase, SqliteQueue, default_db_path


async def main() -> None:
    paths = sorted(str(p) for p in Path("reports").glob("*.txt"))
    async with SqliteDatabase(default_db_path()) as db:
        queue = SqliteQueue(db)  # the queue: sqlite for local work, yours in production (Part 6)
        worker = JobWorker(db, queue, AgentRunner(db), jobs=[summarise_all])  # a worker runs only the jobs you list
        running = asyncio.create_task(worker.run())  # in real life a process of its own: worker.py

        client = JobClient(db, queue)  # the client only writes to the queue and the database
        job_id = await client.submit(summarise_all, Reports(paths=paths))
        async for event in client.events(job_id):  # until the job is final
            print(event.type)  # queued, running, succeeded
        digest = await client.result(summarise_all, job_id)  # read from your result store
        assert isinstance(digest, Digest)  # the job's output model, parsed
        print(len(digest.summaries), "summaries; first:", digest.summaries[0].title)

        await worker.stop()
        await running


if __name__ == "__main__":
    asyncio.run(main())
```
queued
running
succeeded
4 summaries; first: something like the most severe report
```

As two processes (the container shares the data volume), start the worker in one terminal and submit from another:

<!-- code: code/5-1/worker.py -->
```python
"""Chapter 5-1: the worker as a process of its own. It takes jobs from the queue until it is stopped."""

import asyncio

from digest import summarise_all

from featherweight import AgentRunner, JobWorker, SqliteDatabase, SqliteQueue, default_db_path


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db:
        # takes jobs from the queue until it is stopped
        await JobWorker(db, SqliteQueue(db), AgentRunner(db), jobs=[summarise_all]).run()


if __name__ == "__main__":
    asyncio.run(main())
```python
```

```
docker compose run --rm --no-deps book python 5-1/worker.py     # terminal 1: runs until you stop it
docker compose run --rm --no-deps book python 5-1/submit.py     # terminal 2
```

**What to notice**

- **`submit` returns at once.** The client never runs the job: it writes the job and puts a message on the queue. The worker takes the message and runs it.
  `JobWorker(..., jobs=[summarise_all])` runs only the jobs you list.
- **The client and the worker only meet in the queue and the database,** so they can be two processes, or two machines.
- **The result is in your result store.** `client.result` reads the job's output from it. The default is a sqlite store for local work, and Part 6 shows how to
  bring your own.
- **`client.events(job_id)`** streams the job's status changes until it is final. Over SSE it is `sse.encode(client.events(job_id))`: see [events and SSE](../../submitted-jobs.md#events-and-sse).
- **`client.cancel(job_id)`** cancels a queued job at once, and asks a running one to stop.
- **It is the same job as in Part 3.** `run_job(runner, summarise_all, ...)` runs it here, and `client.submit(summarise_all, ...)` has a worker run it: only who runs it, and where, differs.

> **Why a queue and a database.** Nothing connects the two sides directly, so either can restart or move without the other. featherweight ships a sqlite queue for local
> work; in production the queue is yours. See [submitted jobs](https://github.com/aadhithya/featherweight/blob/main/docs/specs/submitted-jobs.md#the-principle) and [the guide](../../submitted-jobs.md).

Next: more than one worker, and a worker that dies.
