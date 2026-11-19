"""A job worker, as a process of its own.

    python examples/jobs/worker.py

It takes jobs from the queue until it is stopped. Submit them from `api.py` (or any other process on the same database).
"""

import asyncio

from shout import shout

import featherweight as ftw


async def main() -> None:
    async with ftw.SqliteDatabase(ftw.default_db_path()) as db:
        worker = ftw.JobWorker(db, ftw.SqliteQueue(db), ftw.AgentRunner(db), jobs=[shout])
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
