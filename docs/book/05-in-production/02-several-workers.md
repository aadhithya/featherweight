<!-- nav:top -->
[Book](../README.md) › [Part 5: In production](../README.md#part-5-in-production) › Several workers  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# Several workers

Run several workers, and lose one without losing the job.

Add as many workers as you like. When one dies, another takes its job.

<!-- code: code/5-2/app.py -->
```python
"""Chapter 5-2: kill a worker in the middle of a job, and another worker takes the job over."""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from featherweight import AgentRunner, JobClient, JobContext, JobWorker, SqliteDatabase, SqliteQueue, job

LEASE = 1.0  # seconds: short, so that this takes seconds. The default is 60.
os.environ["FTW_JOB_LEASE"] = os.environ["FTW_QUEUE_LEASE"] = str(
    LEASE
)  # settings are environment variables: a worker process inherits them


class Nap(BaseModel):
    seconds: float


class Rested(BaseModel):
    rested: float


@job(timeout=30)  # a job that runs longer than this fails with JobTimeout
async def nap(ctx: JobContext, input: Nap) -> Rested:  # a stand-in for a long job: it needs no model
    await asyncio.sleep(input.seconds)
    return Rested(rested=input.seconds)


async def worker(path: str) -> None:
    async with SqliteDatabase(path) as db:
        queue = SqliteQueue(db)
        await JobWorker(db, queue, AgentRunner(db), jobs=[nap]).run()


async def main() -> None:
    path = str(Path(tempfile.mkdtemp()) / "workers.db")
    async with SqliteDatabase(path) as db:
        client = JobClient(db, SqliteQueue(db))
        a = subprocess.Popen([sys.executable, __file__, "worker", path])  # worker A, a process of its own
        job_id = await client.submit(nap, Nap(seconds=2))
        b = None
        async for event in client.events(job_id):
            print(event.type)  # queued, running, lost, running, succeeded
            if event.type == "running" and b is None:
                a.kill()  # SIGKILL: no goodbye, and the lease stops being renewed
                print("worker A killed")
                b = asyncio.create_task(worker(path))  # worker B
        print((await client.status(job_id)).executions, "executions")  # 2
        if b:
            b.cancel()


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        asyncio.run(worker(sys.argv[2]))
    else:
        asyncio.run(main())
```

```text
queued
running
worker A killed
lost
running
succeeded
2 executions
```

**What to notice**

- **A lease.** While a worker runs a job it renews two leases every third of the job's lease (`FTW_JOB_LEASE`, 60 seconds by default, 1 here): the job's, in the database, and its
  message's (`FTW_QUEUE_LEASE`), in the queue. Settings like these are environment variables, and a worker process inherits them. A worker killed with SIGKILL stops renewing them.
- **A takeover.** When the leases run out, the message comes back to a worker, which sees that the job's lease expired and takes the job over. It is the
  **same job** with `executions` one higher, and a `lost` event in its events.
- **A job must be safe to run twice.** A worker that was only paused, and wakes up after a takeover, is refused its writes and stops the job; but what the job's own
  code already did (a mail sent, an API called) is not undone. A resumable job reuses the steps that finished, as in a resume.
- **`timeout`.** A job that runs longer than its `timeout` (600 seconds by default) fails with `JobTimeout`. A heartbeat only shows that a worker is alive, so a hung job needs this.
- **How often a message may come back** is your queue's policy; `SqliteQueue` marks a message dead after `FTW_QUEUE_MAX_DELIVERIES` (3) deliveries.

> **Why the same job.** The job id is what the client holds: its events, its cancel and its result are all keyed by it, so a takeover keeps it. See
> [several workers](../../specs/submitted-jobs.md#several-workers) and [the guide](../../submitted-jobs.md#several-workers).

**Also possible**

- Every setting you can tune, and its `FTW_` variable: [Configuration](../../specs/configuration.md).
- What to use where in production: [a production stack](../../production-stack.md).

Next: an agent that starts another agent and goes on.

<!-- nav:bottom -->
---

[← Submit and a worker](01-submit-and-a-worker.md) · [Part 5: In production](../README.md#part-5-in-production) · [Background jobs →](03-background-jobs.md)
<!-- /nav:bottom -->
