<!-- Copied from docs/book/01-your-first-agent/04-what-happened.md by scripts/build_skill.py. Edit the original, not this file. -->

# What happened

Read the run log: what featherweight recorded about a run.

<!-- code: code/1-4/app.py -->
```python
"""Chapter 1-4: the same run, grouped by a job id, and what the run log recorded."""

import asyncio
from pathlib import Path
from typing import ClassVar, Literal

from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, SqliteDatabase, default_db_path


class Report(AgentInput):  # what goes in: a typed model, checked before the model is called
    text: str


class Summary(AgentOutput):  # also carries `open_questions: list[str]`
    title: str = ""  # every field has a default: a model that does not know is not forced to invent
    # only these values, or None when the report does not say
    severity: Literal["low", "medium", "high"] | None = None
    service: str | None = None
    impact: str = ""
    next_steps: list[str] = []


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"  # how the run log and the traces name the agent
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity or the service is not stated, leave it out and ask in open_questions."
    )
    input_model: ClassVar[type[AgentInput]] = Report  # the contract is set on the class: the same for every run
    output_model: ClassVar[type[AgentOutput]] = Summary


async def main() -> None:
    report = Report(text=Path("reports/01-checkout-outage.txt").read_text())
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        # the class, not an instance: the runner builds the agent
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
        print(result.status.value, result.output)
        for row in await db.list_attempts(
            job_id=job_id
        ):  # one row for each attempt: metadata, never the report or the summary
            print(
                f"attempt {row.attempt}: {row.status.value}, {row.model_name}, tokens={row.input_tokens}+{row.output_tokens}, "
                f"{row.latency_ms or 0:.0f} ms, input_hash={row.input_hash[:8]}"
            )


if __name__ == "__main__":
    asyncio.run(main())
```

```text
SUCCESS open_questions=[] title='Checkout was down' ...
attempt 1: SUCCESS, gemini-3.5-flash-lite, tokens=312+96, 2210 ms, input_hash=2d368048
```

**What to notice**

- **One row for each attempt:** its status, the model, the tokens, the time and the hashes. A retry is its own row (the next chapter).
- **Metadata and nothing else.** The row has the hash of the report, never the report, and not the summary either. The framework stores
  metadata and you bring the storage: where the report and the summary live is your decision.
- **`create_job` gives a job id**, which groups the runs of one piece of work. Jobs get their own part, [Part 3](../03-jobs-and-pipelines/01-what-a-job-is.md).
- **The run log is a `Database`:** `SqliteDatabase` here, `PostgresDatabase` for a deployment, or your own ([Part 6](../06-bring-your-own-storage/01-postgres.md)).
- The file is `/data/book.db`. `make clean` in `docs/book/code` resets it.

> **Why only metadata.** What went through the agent is yours, and so is how it is kept. See [the rule](https://github.com/aadhithya/featherweight/blob/main/docs/specs/persistence.md#the-rule)
> and [the run log](../../persistence.md#what-the-run-log-holds).

**Also possible**

- Log what the runner does: `setup_logging()` and `FTW_LOG_LEVEL`, in [Configuration](../../agents.md#configuration).

Next: a rule the schema cannot say.
