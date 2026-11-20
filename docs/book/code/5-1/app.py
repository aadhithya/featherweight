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
