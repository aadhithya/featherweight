<!-- nav:top -->
[Book](../README.md) › [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) › What a job is  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# What a job is

Turn the agent's work into a job: a declared class that you run with `run_job`.

This part adds jobs to the assistant from Part 1; you can read it without the other parts. `incident.py` in each folder is that assistant, unchanged:
its models and its agent. The new code is only the jobs.

<!-- code: code/3-1/app.py -->
```python
"""Chapter 3-1: a job is a declared class: one that summarises a report, and one that runs the assistant over many."""

import asyncio
from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, SqliteDatabase, job, run_job


@job  # a class made from this function: a name, a version, a typed input and output
async def summarise(ctx: JobContext, input: Report) -> Summary:
    result = await ctx.run(IncidentSummariser, input)  # an agent run, inside the job
    if result.output is None:
        raise RuntimeError(result.error)
    return result.output


class Reports(BaseModel):
    paths: list[str]


class Digest(BaseModel):
    summaries: list[Summary]


@job  # a class made from this function: a name, a version, a typed input and output
async def summarise_all(ctx: JobContext, input: Reports) -> Digest:
    reports = [Report(text=Path(path).read_text()) for path in input.paths]
    results = await ctx.run_many(IncidentSummariser, reports)  # all at once, in order
    failed = [path for path, result in zip(input.paths, results, strict=True) if result.output is None]
    if failed:  # a digest with gaps would look complete: fail, and say which reports
        raise RuntimeError(f"Could not summarise: {', '.join(failed)}")
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
```

Run it: `make book-chapter N=3-1`

```
SUCCESS Checkout was down
SUCCESS ['Checkout was down', 'Sign-in trouble', 'Search was slow', 'Batch was late']
```

**What to notice**

- **A job is a function with a typed input and a typed output.** `@job` turns it into a class with a name, a version and the two models, and `run_job` runs it
  and returns an outcome: `status`, `result` (the output model) and `error`. A job that fails is an outcome, not an exception, as for a run.
- **`ctx.run` and `ctx.run_many` run agents in the job.** They share the job's id, so the runs are recorded under it (the next chapter).
- **The pipeline is plain Python.** `summarise_all` reads files and collects results with a list comprehension: no graph, no special syntax.
- **`SqliteDatabase(":memory:")`** keeps the run log in memory: a quick try with no file, gone when the program ends.
- **When to use a job.** Use one when the work is something you want to name, follow, run again or hand to someone else: it spans several runs, an API
  triggers it, or it can fail halfway and should pick up again. A single agent call in a script needs no job: `runner.run` is enough.
- **What a job gives you that a function does not.** A name and a version, so a change is visible; a status, events and a run log you can read; its output in
  a store you choose; resume after a failure; and the same job on a worker, with no rewrite.

> **Why a class.** A declared job has a name, a version and typed models, so the same job can be run here (`run_job`) or on a worker (Part 5), and be told
> apart when it changes. See [the philosophy](../00-start-here/03-philosophy.md), [the job](../../specs/submitted-jobs.md#the-job), [running a job class](../../specs/jobs.md#running-a-job-class) and [the guide](../../submitted-jobs.md).

Next: what the run recorded.

<!-- nav:bottom -->
---

[← Sub-agents](../02-abilities/04-sub-agents.md) · [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) · [Status and events →](02-status-and-events.md)
<!-- /nav:bottom -->
