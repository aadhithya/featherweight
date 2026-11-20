"""The finished project, with nothing to type: every sample report, summarised on two workers, and the trace."""

import asyncio
from pathlib import Path

from jobs import Digest, Reports, summarise_all
from tracing import print_trace

from featherweight import AgentRunner, JobClient, JobWorker, OTelObserver, SqliteDatabase, SqliteQueue


async def main() -> None:
    paths = sorted(str(p) for p in (Path(__file__).parent.parent / "reports").glob("*.txt"))
    async with SqliteDatabase(":memory:") as db:  # in memory: nothing is kept
        queue = SqliteQueue(db)  # the queue we ship, for one machine; a deployment brings its own (Chapter 6-3)
        runner = AgentRunner(db, observers=[OTelObserver()])  # the observer emits the spans, and stores nothing
        # Two workers on one queue: one takes the job, and the other is ready to take it over if that one dies (Chapter 5-2).
        workers = [JobWorker(db, queue, runner, jobs=[summarise_all]) for _ in range(2)]
        running = [asyncio.create_task(w.run()) for w in workers]

        client = JobClient(db, queue)  # the submitting side: it only meets the workers in the queue and the database
        job_id = await client.submit(summarise_all, Reports(paths=paths))
        async for event in client.events(job_id):
            print("job", event.type)
        digest = await client.result(
            summarise_all, job_id
        )  # read from the result store: the default is sqlite, yours can be any
        if isinstance(digest, Digest):
            for s in digest.summaries:
                print(
                    f"{s.severity or '?':<7}{s.title} | {s.service or '?'} | {s.owner or '?'} | {s.duration_minutes or '?'} min"
                )
        else:
            print("failed:", (await client.status(job_id)).error_type)

        await asyncio.gather(*(w.stop() for w in workers))
        await asyncio.gather(*running)
    print_trace()


if __name__ == "__main__":
    asyncio.run(main())
