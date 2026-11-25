"""Kill a worker in the middle of a job, and watch another one take the job over.

    python examples/jobs/takeover.py

No model and no key. Worker A runs as a process of its own, and is killed with SIGKILL while it holds the job. Its lease runs out,
the message comes back, and worker B takes the job over: the events read queued, running, lost, running, succeeded.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from pydantic import BaseModel  # noqa: E402

from featherweight import AgentRunner, JobClient, JobContext, JobWorker, SqliteDatabase, SqliteQueue, job  # noqa: E402

LEASE = 1.0  # seconds: short, so that this takes seconds. The default is 60.
os.environ["FTW_JOB_LEASE"] = os.environ["FTW_QUEUE_LEASE"] = str(
    LEASE
)  # settings are environment variables: a worker process inherits them


class Nap(BaseModel):
    seconds: float


class Rested(BaseModel):
    rested: float


@job(timeout=30)
async def nap(ctx: JobContext, input: Nap) -> Rested:
    await asyncio.sleep(input.seconds)
    return Rested(rested=input.seconds)


async def worker(path: str) -> None:
    async with SqliteDatabase(path) as db:
        queue = SqliteQueue(db)
        await JobWorker(db, queue, AgentRunner(db), jobs=[nap]).run()


async def main() -> None:
    path = str(Path(tempfile.mkdtemp()) / "takeover.db")
    async with SqliteDatabase(path) as db:
        queue = SqliteQueue(db)
        client = JobClient(db, queue)
        a = subprocess.Popen([sys.executable, __file__, "worker", path])  # worker A, a process of its own
        job_id = await client.submit(nap, Nap(seconds=2))
        b = None
        async for event in client.events(job_id):
            print(event.type)  # queued, running, lost, running, succeeded
            if event.type == "running" and b is None:
                a.kill()  # SIGKILL: no shutdown, no goodbye, and the lease stops being renewed
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
