<!-- nav:top -->
[Book](../README.md) › [Part 7: Reference](../README.md#part-7-reference) › The finished project
<!-- /nav:top -->

# The finished project

Run the project you built, all of it.

The finished project is the last chapter's code: nothing new to learn, only the parts together. It is in `docs/book/code/project/`, and it runs in the
container like every chapter. Put a key in `.env` first ([the container](../code/README.md)).

```text
make book-run     # everything, with nothing to type: two workers, every sample report, a trace
make book-chat    # the interactive part: a chat that starts a researcher in the background
```

(From `docs/book/code/`, the commands are `make run` and `make chat`.)

<!-- code: code/project/run.py -->
```python
"""The finished project, with nothing to type: every sample report, summarised on two workers, and the trace."""

import asyncio
from pathlib import Path

from jobs import Digest, Reports, summarise_all
from tracing import print_trace

from featherweight import AgentRunner, JobClient, JobWorker, OTelObserver, SqliteDatabase, SqliteQueue


async def main() -> None:
    paths = sorted(str(p) for p in (Path(__file__).parent.parent / "reports").glob("*.txt"))
    async with SqliteDatabase(":memory:") as db:  # in memory: nothing is kept
        queue = SqliteQueue(db)  # the queue we ship, for one machine; a deployment brings its own (Chapter 6-3)
        runner = AgentRunner(db, observers=[OTelObserver()])  # the observer emits the spans, and stores nothing
        # Two workers on one queue: one takes the job, and the other is ready to take it over if that one dies (Chapter 5-2).
        workers = [JobWorker(db, queue, runner, jobs=[summarise_all]) for _ in range(2)]
        running = [asyncio.create_task(w.run()) for w in workers]

        client = JobClient(db, queue)  # the submitting side: it only meets the workers in the queue and the database
        job_id = await client.submit(summarise_all, Reports(paths=paths))
        async for event in client.events(job_id):
            print("job", event.type)
        digest = await client.result(
            summarise_all, job_id
        )  # read from the result store: the default is sqlite, yours can be any
        if isinstance(digest, Digest):
            for s in digest.summaries:
                print(
                    f"{s.severity or '?':<7}{s.title} | {s.service or '?'} | {s.owner or '?'} | {s.duration_minutes or '?'} min"
                )
        else:
            print("failed:", (await client.status(job_id)).error_type)

        await asyncio.gather(*(w.stop() for w in workers))
        await asyncio.gather(*running)
    print_trace()


if __name__ == "__main__":
    asyncio.run(main())
```

```text
job queued
job running
job succeeded
high   Checkout outage | checkout | payments team, on call: Ana | 47 min
medium Sign-in trouble | ? | ? | ? min
...
trace:
  1 invoke_workflow spans
  4 invoke_agent spans
  4 chat spans
  2 execute_tool spans
```

The output is something like this: the summaries come from the model.

## How the parts fit

| File | What it is | Taught in |
|---|---|---|
| `incident.py` | The summariser, with a tool, an MCP server, a skill and a sub-agent | [Part 1](../01-your-first-agent/02-a-typed-agent.md), [Part 2](../02-abilities/01-tools.md) |
| `jobs.py` | The pipeline over every report, as a job | [Part 3](../03-jobs-and-pipelines/01-what-a-job-is.md) |
| `assistant.py`, `chat.py` | The chat, the background researcher and the session that wakes it | [Part 4](../04-chat/01-a-streaming-agent.md), [5-3](../05-in-production/03-background-agents.md) |
| `run.py` | A client, a queue and two workers | [5-1](../05-in-production/01-submit-and-a-worker.md), [5-2](../05-in-production/02-several-workers.md) |
| `tracing.py` | The spans, collected in memory | [5-4](../05-in-production/04-observability.md) |
| `severity-rubric/`, `../services_server.py`, `../reports/` | The skill, the MCP server and the sample reports | [2-2](../02-abilities/02-mcp.md), [2-3](../02-abilities/03-skills.md) |

## What to notice

- The job fails if any report fails, and says which. A digest with a gap in it would look complete.
- Where you could bring your own, it names the default: `SqliteDatabase`, `SqliteQueue`, the default result store. Swap them with [Part 6](../06-bring-your-own-storage/01-postgres.md).
- The database is in memory (`:memory:`), so nothing is kept between runs.

> **Why** the project is the last chapter's code and not a second app: what you learned is what you ship.

<!-- nav:bottom -->
---

[← The glossary](03-the-glossary.md) · [Part 7: Reference](../README.md#part-7-reference)
<!-- /nav:bottom -->
