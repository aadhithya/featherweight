"""Chapter 3-3: the job's output goes to the result store that you choose; featherweight keeps no results of its own."""

import asyncio
from pathlib import Path

from incident import IncidentSummariser, Report, Summary

from featherweight import (
    AgentRunner,
    JobClient,
    JobContext,
    SqliteDatabase,
    SqliteQueue,
    SqliteResultStore,
    job,
    run_job,
)


@job
async def summarise(ctx: JobContext, input: Report) -> Summary:
    result = await ctx.run(IncidentSummariser, input)
    if result.output is None:
        raise RuntimeError(result.error)
    return result.output


async def main() -> None:
    report = Report(text=Path("reports/01-checkout-outage.txt").read_text())
    async with SqliteDatabase(":memory:") as db:
        store = SqliteResultStore(db)  # a result store of ours: a sqlite one, for local work. Yours goes here
        outcome = await run_job(AgentRunner(db, results=store), summarise, report)
        print("returned:", outcome.result.title if outcome.result else outcome.error)

        client = JobClient(db, SqliteQueue(db), store)  # how a client, in another process, reads it (Part 5)
        stored = await client.result(summarise, outcome.job_id)
        print("in the store:", stored)

        outcome = await run_job(AgentRunner(db, results=None), summarise, report)  # no store: nothing is kept
        print("results=None, still returned:", outcome.result is not None)


if __name__ == "__main__":
    asyncio.run(main())
