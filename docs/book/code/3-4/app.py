"""Chapter 3-4: a resumable job that dies after its summaries, and a second job that resumes it."""

import asyncio
from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, SqliteDatabase, job, run_job

RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


class Reports(BaseModel):
    paths: list[str]
    crash: bool = False  # to show a failure: the job dies after the summaries


class Digest(BaseModel):
    summaries: list[Summary]


@job
async def summarise_all(ctx: JobContext, input: Reports) -> Digest:
    reports = [Report(text=Path(path).read_text()) for path in input.paths]
    results = await ctx.run_many(IncidentSummariser, reports)
    if input.crash:
        raise RuntimeError("simulated crash: the process died after the summaries")
    summaries: list[Summary] = [r.output for r in results if r.output]
    summaries.sort(key=lambda s: RANK[s.severity])  # plain Python, the same each time
    return Digest(summaries=summaries)


async def show(db, label: str, job_id: str) -> None:
    print(label)
    for run in await db.list_runs(job_id=job_id):
        print(f"  {run.status.value:<8} {'reused: no model call' if run.reused_from_agent_run_id else 'model call'}")


async def main() -> None:
    paths = sorted(str(p) for p in Path("reports").glob("*.txt"))
    async with SqliteDatabase(":memory:") as db:
        runner = AgentRunner(db)
        first = await run_job(runner, summarise_all, Reports(paths=paths, crash=True), resumable=True)
        print("first:", first.status.value, first.error)
        await show(db, "first run", first.job_id)
        second = await run_job(runner, summarise_all, Reports(paths=paths), resume_from=first.job_id)
        print("second:", second.status.value)
        await show(db, "second run", second.job_id)


if __name__ == "__main__":
    asyncio.run(main())
