# A pipeline of two agents

A pipeline is a job: a function with a typed input and output. Order and branching are plain Python. Every agent it runs with `ctx`
belongs to the job, so they share its run log and its trace.

```python
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, Status, SqliteDatabase, job, run_job

# PeopleExtractor and ExtractInput: the agent of Getting started (../getting-started.md#an-agent)


class Texts(BaseModel):
    texts: list[str]


class People(BaseModel):
    people: list[str]


@job
async def find_people(ctx: JobContext, input: Texts) -> People:
    results = await ctx.run_many(PeopleExtractor, [ExtractInput(text=t) for t in input.texts])
    people = []
    for result in results:
        if result.status == Status.SUCCESS:  # decide here what a failed run means: skip, stop, ask again
            people.extend(result.output.people)
    return People(people=sorted(set(people)))


async def main():
    async with SqliteDatabase("featherweight.db") as db:  # or ":memory:" for no file
        outcome = await run_job(AgentRunner(db), find_people, Texts(texts=["Anna met Tom.", "Li came."]))
        # outcome.status, outcome.result (a People) on success, outcome.error on failure
```

`run_job` creates the job, runs it and records `SUCCESS` or `FAILED`. A job that raises comes back as a failed outcome, not an
exception. The same job can be submitted to a queue and run by a worker ([submitted jobs](../submitted-jobs.md)).
