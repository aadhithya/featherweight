<!-- nav:top -->
[Book](../README.md) › [Part 1: Your first agent](../README.md#part-1-your-first-agent) › Failures and check_output
<!-- /nav:top -->

# Failures and check_output

Reject an answer the schema cannot catch, and see what a failed attempt looks like.

<!-- code: code/1-5/app.py -->
```python
"""Chapter 1-5: a rule the schema cannot say, and what a rejected attempt looks like."""

import asyncio
from pathlib import Path
from typing import ClassVar, Literal

from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, SqliteDatabase, default_db_path


class Report(AgentInput):
    text: str


class Summary(AgentOutput):  # also carries `open_questions: list[str]`
    title: str = ""
    severity: Literal["low", "medium", "high"] | None = None
    service: str | None = None
    impact: str = ""
    next_steps: list[str] = []


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "1"
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity or the service is not stated, leave it out and ask in open_questions."
    )
    input_model: ClassVar[type[AgentInput]] = Report
    output_model: ClassVar[type[AgentOutput]] = Summary

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject the attempt
        if output.service and output.service.lower() not in agent_input.text.lower():
            raise ValueError(f"The report does not name the service {output.service!r}")


async def main() -> None:
    report = Report(text=Path("reports/02-sign-in-trouble.txt").read_text())  # this report names no service
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
        print(result.status.value, f"attempts={result.attempts}", result.output if result.output else result.error)
        for row in await db.list_runs(job_id=job_id):
            print(f"attempt {row.attempt}: {row.status.value} {row.error_type or ''}")


if __name__ == "__main__":
    asyncio.run(main())
```

```text
SUCCESS attempts=2 open_questions=['What was the cause?', ...] title='Sign-in trouble' severity=None service=None ...
attempt 1: FAILED ValueError
attempt 2: SUCCESS
```

**What to notice**

- **`check_output(agent_input, output)`** holds rules between the input and the output, which a schema cannot express. Raise `ValueError` to
  reject the attempt.
- **A rejected attempt is retried**, up to `max_attempts` (3). The retry samples again with the same input: the error is in the log
  (`error_type`) and is not sent back to the model.
- **If every attempt fails, you get a `FAILED` result**, with the last error, and not an exception. Your code decides what that means.
- A reply that breaks the schema (a wrong type, a value outside a `Literal`) is corrected within the same attempt.

> **Why it works this way.** Each attempt is a row, so a failure can be diagnosed from the log. See
> [attempts and retries](../../specs/runner.md#attempts-and-retries) and [checking the output](../../agents.md#checking-the-output).

Next: choose the model.

<!-- nav:bottom -->
---

[← What happened](04-what-happened.md) · [Part 1: Your first agent](../README.md#part-1-your-first-agent) · [Choosing a model →](06-choosing-a-model.md)
<!-- /nav:bottom -->
