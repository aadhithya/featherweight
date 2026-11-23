"""A worker, as a process of its own.

    python examples/jobs/worker.py

It takes jobs from the queue until it is stopped. Submit them from `api.py` (or any other process on the same database).
"""

import asyncio

from shout import shout

from featherweight import AgentRunner, JobWorker, SqliteDatabase, SqliteQueue, default_db_path


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db:
        worker = JobWorker(db, SqliteQueue(db), AgentRunner(db), jobs=[shout])
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
