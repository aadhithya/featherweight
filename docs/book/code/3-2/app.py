"""Chapter 3-2: a job has a status and events, and its agent runs are in the run log."""

import asyncio
from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, SqliteDatabase, default_db_path, job, run_job


class Reports(BaseModel):
    paths: list[str]


class Digest(BaseModel):
    summaries: list[Summary]


@job  # a class made from this function: a name, a version, a typed input and output
async def summarise_all(ctx: JobContext, input: Reports) -> Digest:
    reports = [Report(text=Path(path).read_text()) for path in input.paths]
    results = await ctx.run_many(IncidentSummariser, reports)
    failed = [path for path, result in zip(input.paths, results, strict=True) if result.output is None]
    if failed:  # a digest with gaps would look complete: fail, and say which reports
        raise RuntimeError(f"Could not summarise: {', '.join(failed)}")
    return Digest(summaries=[r.output for r in results if r.output])


async def main() -> None:
    paths = sorted(str(p) for p in Path("reports").glob("*.txt"))
    async with SqliteDatabase(default_db_path()) as db:
        outcome = await run_job(AgentRunner(db), summarise_all, Reports(paths=paths))
        job_row = await db.get_job(outcome.job_id)
        assert job_row  # the job is there
        print("job:", job_row.name, job_row.status.value, f"version {job_row.job_version}")
        print("events:", [event.type for event in await db.list_job_events(outcome.job_id)])
        for run in await db.list_runs(job_id=outcome.job_id):  # one row for each attempt of each agent run
            print(
                f"  {run.agent_name} attempt {run.attempt}: {run.status.value}, tokens={run.input_tokens}+{run.output_tokens}"
            )


if __name__ == "__main__":
    asyncio.run(main())
