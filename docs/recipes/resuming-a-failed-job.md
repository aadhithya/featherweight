# Resuming a failed job

Run a job again after it failed, without paying for the steps that worked. Create the first job with `resumable=True`, which keeps the outputs of its steps, and point the second run at it.

```python
import asyncio

from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, SqliteDatabase, Status, default_db_path, job, run_job

# PeopleExtractor and ExtractInput: the agent of Getting started (../getting-started.md#an-agent)


class Texts(BaseModel):
    texts: list[str]


class People(BaseModel):
    people: list[str]


@job
async def find_people(ctx: JobContext, input: Texts) -> People:
    results = await ctx.run_many(PeopleExtractor, [ExtractInput(text=t) for t in input.texts])
    for result in results:
        if result.status != Status.SUCCESS:
            raise RuntimeError(result.error)  # a failed step fails the job, so that it can be resumed
    return People(people=sorted({p for result in results for p in result.output.people}))


async def main() -> None:
    texts = Texts(texts=["Anna met Tom.", "Li came."])
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        first = await run_job(runner, find_people, texts, resumable=True)
        outcome = await run_job(runner, find_people, texts, resume_from=first.job_id)
        print(first.status.value, "then", outcome.status.value, outcome.result)


if __name__ == "__main__":
    asyncio.run(main())
```

Runs that succeeded with the same agent and input are answered from the kept results (`result.reused`, zero tokens). Keep the code between agents deterministic (no clock, no `random`), and do not rely on state outside the result store for a resume.
