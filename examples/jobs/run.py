"""Submit a job and run a worker for it, in one script.

    python examples/jobs/run.py

No model and no key. The client and the worker only meet in the queue and the database, so they could be two processes: see
`worker.py` and `api.py`.
"""

import asyncio

from shout import Text, shout

from featherweight import AgentRunner, JobClient, JobWorker, SqliteDatabase, SqliteQueue, default_db_path


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db:
        queue = SqliteQueue(db)
        worker = JobWorker(db, queue, AgentRunner(db), jobs=[shout])
        client = JobClient(db, queue)

        running = asyncio.create_task(worker.run())
        job_id = await client.submit(shout, Text(text="hello"))
        async for event in client.events(job_id):
            print(event.type)  # queued, running, succeeded
        print(await client.result(shout, job_id))  # Shouted(shouted='HELLO'), read from the result store
        await worker.stop()
        await running


if __name__ == "__main__":
    asyncio.run(main())
