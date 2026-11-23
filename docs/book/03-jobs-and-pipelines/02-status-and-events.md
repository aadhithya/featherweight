<!-- nav:top -->
[Book](../README.md) › [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) › Status and events  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# Status and events

Follow a job through its status and its events.

<!-- code: code/3-2/app.py -->
```python
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
        for run in await db.list_attempts(job_id=outcome.job_id):  # one row for each attempt of each agent run
            print(
                f"  {run.agent_name} attempt {run.attempt}: {run.status.value}, tokens={run.input_tokens}+{run.output_tokens}"
            )


if __name__ == "__main__":
    asyncio.run(main())
```

Run it: `make book-chapter N=3-2`

```
job: summarise_all SUCCESS version 1
events: ['queued', 'running', 'succeeded']
  incident_summariser attempt 1: SUCCESS, tokens=312+96
  ... (one row for each report)
```

**What to notice**

- **The job row** has its name, its status and its version. Reading it is `db.get_job(job_id)`.
- **Events** are the history of the job's status, one for each change: `queued`, `running`, then `succeeded` or `failed`. The database writes each event in the
  same step as the change, so they cannot disagree. A client in another process follows them as a stream (Part 5).
- **The run log** (`db.list_attempts(job_id=...)`) has one row for each attempt of each agent run in the job: status, model, tokens, time and hashes.
- **Metadata and nothing else.** No report, no summary and no error text is in any of it: the framework stores metadata, and you bring the storage.

> **Why metadata.** What went through the job is yours, and so is how it is kept. See [the rule](../../specs/persistence.md#the-rule) and
> [job events](../../specs/submitted-jobs.md#job-events).

Next: where the job's output goes.

<!-- nav:bottom -->
---

[← What a job is](01-what-a-job-is.md) · [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) · [Results and the result store →](03-results-and-the-result-store.md)
<!-- /nav:bottom -->
