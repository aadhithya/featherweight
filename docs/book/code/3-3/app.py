"""Chapter 3-3: the job's output goes to the result store that you choose; featherweight keeps no results of its own."""

import asyncio
from pathlib import Path

from incident import IncidentSummariser, Report, Summary

from featherweight import AgentRunner, JobContext, ResultKey, SqliteDatabase, SqliteResultStore, job, run_job


@job  # a class made from this function: a name, a version, a typed input and output
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

        row = await db.get_job(outcome.job_id)
        assert row and row.job_hash and row.input_hash  # a job class has both hashes
        # the key of a stored result: the job's id, the job's hash (the field is called agent_hash) and the input's hash
        key = ResultKey(job_id=outcome.job_id, agent_hash=row.job_hash, input_hash=row.input_hash)
        print("in the store:", await store.get(key))  # what a later reader finds: Part 5's client does this for you

        outcome = await run_job(AgentRunner(db, results=None), summarise, report)  # no store: nothing is kept
        print("results=None, still returned:", outcome.result is not None)


if __name__ == "__main__":
    asyncio.run(main())
