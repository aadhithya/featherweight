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
