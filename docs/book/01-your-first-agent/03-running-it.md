<!-- nav:top -->
[Book](../README.md) › [Part 1: Your first agent](../README.md#part-1-your-first-agent) › Running it
<!-- /nav:top -->

# Running it

Run the agent on a report, and read the result.

<!-- code: code/1-3/app.py -->
```python
"""Chapter 1-3: run the agent on a report, and read the result."""

import asyncio
from pathlib import Path
from typing import ClassVar, Literal

from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, RunStatus, SqliteDatabase, default_db_path


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


async def main() -> None:
    report = Report(text=Path("reports/01-checkout-outage.txt").read_text())
    async with SqliteDatabase(default_db_path()) as db:  # the run log: a sqlite file, /data/book.db in the container
        result = await AgentRunner(db).run(IncidentSummariser, report)
    if result.status == RunStatus.SUCCESS:
        print(result.output)
    else:
        print("failed:", result.error)  # a failed run is a value, not an exception
    print(result.status.value, f"attempts={result.attempts}", f"tokens={result.input_tokens}+{result.output_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
```

```text
open_questions=[] title='Checkout was down' severity='high' service='checkout' impact='About 1,200 orders affected ...' next_steps=[...]
SUCCESS attempts=1 tokens=312+96
```

**What to notice**

- **`run` takes the class, not an instance.** The runner builds the agent, and returns an `AgentResult`: `status`, `output`, `error`,
  `attempts` and the tokens.
- **A failed run is a value, not an exception.** You check `result.status`. (A wrong input type does raise: that is a bug in your code.)
- **`SqliteDatabase(default_db_path())`** is the run log. In the container it is `/data/book.db`, in a volume that survives between runs.
  The next chapter reads it.

> **Why a value.** A pipeline decides what a failure means. See [the philosophy](../00-start-here/03-philosophy.md) and
> [the result](../../specs/runner.md#the-result).

Next: what the run log recorded.

<!-- nav:bottom -->
---

[← A typed agent](02-a-typed-agent.md) · [Part 1: Your first agent](../README.md#part-1-your-first-agent) · [What happened →](04-what-happened.md)
<!-- /nav:bottom -->
