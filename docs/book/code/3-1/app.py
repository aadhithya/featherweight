"""Chapter 3-1: a job is a declared class: one that summarises a report, and one that runs the assistant over many."""

import asyncio
from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, SqliteDatabase, job, run_job


@job
async def summarise(ctx: JobContext, input: Report) -> Summary:
    result = await ctx.run(IncidentSummariser, input)  # an agent run, inside the job
    if result.output is None:
        raise RuntimeError(result.error)
    return result.output


class Reports(BaseModel):
    paths: list[str]


class Digest(BaseModel):
    summaries: list[Summary]


@job
async def summarise_all(ctx: JobContext, input: Reports) -> Digest:
    reports = [Report(text=Path(path).read_text()) for path in input.paths]
    results = await ctx.run_many(IncidentSummariser, reports)  # all at once, in order
    return Digest(summaries=[r.output for r in results if r.output])


async def main() -> None:
    async with SqliteDatabase(":memory:") as db:  # no file: a quick try
        runner = AgentRunner(db)
        one = await run_job(runner, summarise, Report(text=Path("reports/01-checkout-outage.txt").read_text()))
        print(one.status.value, one.result.title if one.result else one.error)
        paths = sorted(str(p) for p in Path("reports").glob("*.txt"))
        many = await run_job(runner, summarise_all, Reports(paths=paths))
        print(many.status.value, [s.title for s in many.result.summaries] if many.result else many.error)


if __name__ == "__main__":
    asyncio.run(main())
