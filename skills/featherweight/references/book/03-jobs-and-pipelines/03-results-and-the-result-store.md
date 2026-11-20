<!-- Copied from docs/book/03-jobs-and-pipelines/03-results-and-the-result-store.md by scripts/build_skill.py. Edit the original, not this file. -->

# Results and the result store

Keep what a job produced in a result store that you own.

<!-- code: code/3-3/app.py -->
```python
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
```

Run it: `make book-chapter N=3-3`

```
returned: Checkout was down
in the store: {'open_questions': [], 'title': 'Checkout was down', 'severity': 'high', ...}
results=None, still returned: True
```

**What to notice**

- **`run_job` returns the output**, and also writes it to the runner's result store, if the runner has one. The store is a `results=` argument.
- **`SqliteResultStore`** is for local work: it keeps results in the same sqlite file as the run log. In a deployment the store is yours: a bucket, a table,
  a database for each customer. Part 6 writes one.
- **A later reader gets it from the store, by key:** the job's id, the job's hash and the input's hash. In [Part 5](../05-in-production/01-submit-and-a-worker.md) a
  client does this for you and parses it into the job's output model.
- **`results=None` keeps nothing.** The outcome still has the output, but it is gone when the program ends, and there is nothing for a later job to
  [resume](04-resume.md) from.

> **Why no results of our own.** Results are application data, so you decide where they live. See [the rule](https://github.com/aadhithya/featherweight/blob/main/docs/specs/persistence.md#the-rule) and
> [the result store](https://github.com/aadhithya/featherweight/blob/main/docs/specs/persistence.md#the-result-store).

Next: run a failed job again without paying twice.
