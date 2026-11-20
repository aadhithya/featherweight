"""Chapter 6-1: the same run log, in Postgres. One line changes."""

import asyncio
import os

from featherweight import PostgresDatabase

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")


async def main() -> None:
    # Databases: SqliteDatabase (local), PostgresDatabase (a deployment), or your own (Chapter 6-4). The runner does not change.
    async with PostgresDatabase(DSN) as db:  # it was SqliteDatabase(...): the tables are made in a schema of their own
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        job = await db.get_job(job_id)
        print(job.status.value if job else "no job")  # INIT


if __name__ == "__main__":
    asyncio.run(main())
