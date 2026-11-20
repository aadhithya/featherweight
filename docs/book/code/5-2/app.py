"""Chapter 5-2: kill a worker in the middle of a job, and another worker takes the job over."""

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from featherweight import AgentRunner, JobClient, JobContext, JobWorker, SqliteDatabase, SqliteQueue, job

LEASE = 1.0  # seconds: short, so that this takes seconds. The default is 60.


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
        queue = SqliteQueue(db, lease=LEASE)
        await JobWorker(db, queue, AgentRunner(db), jobs=[nap], lease=LEASE).run()


async def main() -> None:
    path = str(Path(tempfile.mkdtemp()) / "workers.db")
    async with SqliteDatabase(path) as db:
        client = JobClient(db, SqliteQueue(db, lease=LEASE))
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
