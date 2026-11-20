"""Chapter 5-1: the client, in another process: submit the job, follow it, read the result."""

import asyncio
from pathlib import Path

from digest import Digest, Reports, summarise_all

from featherweight import JobClient, SqliteDatabase, SqliteQueue, default_db_path


async def main() -> None:
    paths = sorted(str(p) for p in Path("reports").glob("*.txt"))
    async with SqliteDatabase(default_db_path()) as db:
        client = JobClient(db, SqliteQueue(db))
        job_id = await client.submit(summarise_all, Reports(paths=paths))
        async for event in client.events(job_id):
            print(event.type)
        digest = await client.result(summarise_all, job_id)
        assert isinstance(digest, Digest)
        print(len(digest.summaries), "summaries; first:", digest.summaries[0].title)


if __name__ == "__main__":
    asyncio.run(main())
