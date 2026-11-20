<!-- nav:top -->
[Book](../README.md) › [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) › Resume
<!-- /nav:top -->

# Resume

Run a failed job again without paying for the steps that worked.

<!-- code: code/3-4/app.py -->
```python
"""Chapter 3-4: a resumable job that dies after its summaries, and a second job that resumes it."""

import asyncio
from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, SqliteDatabase, job, run_job

RANK = {"high": 0, "medium": 1, "low": 2, None: 3}  # most severe first; no severity last


class Reports(BaseModel):
    paths: list[str]
    crash: bool = False  # to show a failure: the job dies after the summaries


class Digest(BaseModel):
    summaries: list[Summary]


@job  # a class made from this function: a name, a version, a typed input and output
async def summarise_all(ctx: JobContext, input: Reports) -> Digest:
    reports = [Report(text=Path(path).read_text()) for path in input.paths]
    results = await ctx.run_many(IncidentSummariser, reports)
    failed = [path for path, result in zip(input.paths, results, strict=True) if result.output is None]
    if failed:  # a digest with gaps would look complete: fail, and say which reports
        raise RuntimeError(f"Could not summarise: {', '.join(failed)}")
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
        # resumable: keep the results of the steps, so that a later job can resume from this one
        first = await run_job(runner, summarise_all, Reports(paths=paths, crash=True), resumable=True)
        print("first:", first.status.value, first.error)
        await show(db, "first run", first.job_id)
        # a new job: the steps that finished are not run again
        second = await run_job(runner, summarise_all, Reports(paths=paths), resume_from=first.job_id)
        print("second:", second.status.value)
        await show(db, "second run", second.job_id)


if __name__ == "__main__":
    asyncio.run(main())
```

Run it: `make book-chapter N=3-4`. The first run fails on purpose, and featherweight logs that failure, with its traceback, to stderr: that is expected.

```
first: FAILED RuntimeError: simulated crash: the process died after the summaries
first run
  SUCCESS  model call
  ... (four runs)
second: SUCCESS
second run
  SUCCESS  reused: no model call
  ... (four runs)
```

**What to notice**

- **`resumable=True`** is decided before the job runs: it keeps the output of each step in the result store, so that a later job can use it. A job that did
  not keep them cannot be resumed.
- **`resume_from=first.job_id`** starts a **new job** that points to the failed one. The agent runs that already succeeded, with the same agent and the same
  input, are answered from the kept results: no model call, no tokens. The failed job stays as a record.
- **The code between the agents must give the same inputs each time**, which is why the digest is sorted with plain, deterministic Python. If the code took
  another path, the inputs would differ and the agents would run again.
- **The crash is simulated** with a flag in the input. In real life it is a process that dies or a step that raises.

> **Why a new job.** A resume is you running a job that ended again, so it is its own job with its own trace. See
> [resuming a job](../../specs/jobs-and-tracing.md#resuming-a-job) and [the resume example](../../../examples/resume/README.md).

Next: the assistant talks. Part 4 makes it a chat.

<!-- nav:bottom -->
---

[← Results and the result store](03-results-and-the-result-store.md) · [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) · [A streaming agent →](../04-chat/01-a-streaming-agent.md)
<!-- /nav:bottom -->
