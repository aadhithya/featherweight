<!-- Copied from docs/book/02-abilities/01-tools.md by scripts/build_skill.py. Edit the original, not this file. -->

# Tools

Give the agent a tool it can call.

This part adds abilities to the assistant from Part 1. Parts 2, 3 and 4 do not depend on each other: you can read this one without the others.

Every ability in this part is an allow-list: the agent has what its declaration names and nothing else, and each one is a step you take on purpose (see [the philosophy](../00-start-here/03-philosophy.md)).

<!-- code: code/2-1/app.py -->
```python
"""Chapter 2-1: a tool. The summariser works out how long the incident lasted."""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal

from langchain_core.tools import BaseTool, tool

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    default_db_path,
)


@tool
def minutes_between(start: str, end: str) -> str:
    """The minutes between two timestamps, each written as 'YYYY-MM-DD HH:MM'."""
    return str(int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // 60))


class Report(AgentInput):  # what goes in: a typed model, checked before the model is called
    text: str


class Summary(AgentOutput):  # also carries `open_questions: list[str]`
    title: str = ""  # every field has a default: a model that does not know is not forced to invent
    # only these values, or None when the report does not say
    severity: Literal["low", "medium", "high"] | None = None
    service: str | None = None
    impact: str = ""
    duration_minutes: int | None = None  # new: the model cannot subtract timestamps reliably, the tool can
    next_steps: list[str] = []


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"  # how the run log and the traces name the agent
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "2"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity or the service is not stated, leave it out and ask in open_questions. "
        "If the report gives a start and an end time, use minutes_between for duration_minutes."
    )
    # the model is a setting of the agent
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    tools: list[BaseTool] = [minutes_between]  # an allow-list: only these tools are bound to the model
    input_model: ClassVar[type[AgentInput]] = Report  # the contract is set on the class: the same for every run
    output_model: ClassVar[type[AgentOutput]] = Summary

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject the attempt
        if output.service and output.service.lower() not in agent_input.text.lower():
            raise ValueError(f"The report does not name the service {output.service!r}")


async def main(path: str = "reports/04-batch-delay.txt") -> None:
    report = Report(text=Path(path).read_text())
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        # the class, not an instance: the runner builds the agent
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
    print(result.status.value, result.model_name, result.output or result.error)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
```

Run it with `make book-chapter N=2-1`. It reads `reports/04-batch-delay.txt`, which says when the batch started and when it ended. Something like:

```
SUCCESS gemini-3.5-flash-lite open_questions=[] title='Nightly batch was late' severity='low' service='batch' impact='Reports were a day old' duration_minutes=1170 next_steps=['Alert when the disk is 80% full']
```

**What to notice**

- **A tool is a plain function** with LangChain's `@tool`: featherweight is built on LangChain, so its tools are LangChain tools. The model reads its name, its docstring and its argument names, so write them for the model.
- **`tools` is an allow-list, and a guardrail.** Only the tools the agent declares are bound to the model, so nothing else is reachable. The runner checks the list before the model sees it, together with any tools the runner adds.
- **The tool does the subtraction, not the model.** The report says 14:05 on 2 March and 09:35 on 3 March: 1170 minutes. `duration_minutes` is a new field of `Summary`, so the answer is typed like the rest.
- **Ready-made tools** (`calculate`, `current_datetime`, `days_between`, `add_to_date`) are in `featherweight.tools`: see [the tools guide](../../agents.md#tools).

> **Why a list on the class.** The tools are part of what the agent is, and the run log keeps a hash of them, so a change shows. See
> [tools on an agent](https://github.com/aadhithya/featherweight/blob/main/docs/specs/tools.md#tools-on-an-agent).

Next: tools that live in another process.
