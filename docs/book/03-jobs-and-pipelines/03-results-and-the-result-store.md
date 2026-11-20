<!-- nav:top -->
[Book](../README.md) › [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) › Results and the result store
<!-- /nav:top -->

# Results and the result store

Keep what a job produced in a result store that you own.

<!-- code: code/3-3/app.py -->
```python
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
```

Run it: `make book-chapter N=3-3`

```
returned: Checkout was down
in the store: title='Checkout was down' severity='high' ...
results=None, still returned: True
```

**What to notice**

- **`run_job` returns the output**, and also writes it to the runner's result store, if the runner has one. The store is a `results=` argument.
- **`SqliteResultStore`** is for local work: it keeps results in the same sqlite file as the run log. In a deployment the store is yours: a bucket, a table,
  a database for each customer. Part 6 writes one.
- **A client reads the result back** with `JobClient.result(job, job_id)`, which parses what the store holds into the job's output model. Part 5 uses it
  from another process.
- **`results=None` keeps nothing.** The outcome still has the output, but it is gone when the program ends, and there is nothing for a later job to
  [resume](04-resume.md) from.

> **Why no results of our own.** Results are application data, so you decide where they live. See [the rule](../../specs/persistence.md#the-rule) and
> [the result store](../../specs/persistence.md#the-result-store).

Next: run a failed job again without paying twice.

<!-- nav:bottom -->
---

[← Status and events](02-status-and-events.md) · [Part 3: Jobs and pipelines](../README.md#part-3-jobs-and-pipelines) · [Resume →](04-resume.md)
<!-- /nav:bottom -->
