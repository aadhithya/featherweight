<!-- nav:top -->
[Book](../README.md) › [Part 2: Abilities](../README.md#part-2-abilities) › Skills  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# Skills

Teach it a procedure with a skill.

<!-- code: code/2-3/app.py -->
```python
"""Chapter 2-3: a skill. The summariser reads a severity rubric when the report does not state the severity."""

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
    HttpMCPServer,
    ModelSettings,
    Skill,
    SqliteDatabase,
    StdioMCPServer,
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
    duration_minutes: int | None = None
    owner: str | None = None  # new: the answer of the MCP server
    next_steps: list[str] = []


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"  # how the run log and the traces name the agent
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "4"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity is not stated, decide it with your skill. If the service is not stated, leave it out and ask in open_questions. "
        "If the report gives a start and an end time, use minutes_between for duration_minutes. "
        "If the report names a service, find who owns it with your tools and put the team in owner."
    )
    # the model is a setting of the agent
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    skills: list[Skill] = [Skill.from_directory("2-3/severity-rubric")]  # read on demand, not in every prompt
    tools: list[BaseTool] = [minutes_between]  # an allow-list: only these tools are bound to the model
    mcp_servers: list[StdioMCPServer | HttpMCPServer] = [  # the runner starts this server when the agent needs it
        StdioMCPServer(
            name="services",
            command=sys.executable,
            args=("services_server.py",),
            description="Who owns a service, and who is on call",
        )
    ]
    input_model: ClassVar[type[AgentInput]] = Report  # the contract is set on the class: the same for every run
    output_model: ClassVar[type[AgentOutput]] = Summary

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject the attempt
        if output.service and output.service.lower() not in agent_input.text.lower():
            raise ValueError(f"The report does not name the service {output.service!r}")


async def main(path: str = "reports/02-sign-in-trouble.txt") -> None:
    report = Report(text=Path(path).read_text())
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        # the class, not an instance: the runner builds the agent
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
    print(result.status.value, result.model_name, result.output or result.error)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
```

The skill is a folder next to the code:

<!-- code: code/2-3/severity-rubric/SKILL.md -->
```markdown
---
name: severity-rubric
description: Decides the severity of an incident (low, medium or high). Use when an incident report does not state its severity.
---

# Severity rubric

Pick the highest level that fits.

- **high**: customers cannot do what they came for (pay, sign in, reach the product), or data was lost.
- **medium**: the product works but is slow or partly broken, or only some customers are affected.
- **low**: no customer noticed, or only internal work was delayed.

If the report gives too little to choose, leave the severity out and ask in `open_questions`.
Worked examples are in `references/examples.md`.
```

Run the chapter with `make book-chapter N=2-3`. It reads `reports/02-sign-in-trouble.txt`, which states no severity. Something like:

```
SUCCESS gemini-3.5-flash-lite open_questions=['What was the cause?'] title='Sign-in trouble' severity='medium' service=None ...
```

**What to notice**

- **A skill is a folder with a `SKILL.md`.** Its `name` and `description` are always in the prompt, and the agent reads the rest, with `get_skill`,
  only when a skill applies. Other files (`references/examples.md`) are read when the instructions point to them. An unused skill costs a line of prompt.
- **`Skill.from_directory`** takes the folder, relative to where the code runs (`/code` in the container).
- **The format is the Agent Skills specification,** shared with other tools. See [the skills guide](../../skills.md) for what is supported.
- **Nothing runs from a skill.** A skill is text for the agent to read; it executes no code.

> **Why a skill and not a longer prompt.** A rubric in the prompt costs tokens in every run, even the ones that do not need it. See
> [how an agent uses a skill](../../specs/skills.md#how-an-agent-uses-a-skill).

Next: handing part of the work to another agent.

<!-- nav:bottom -->
---

[← MCP](02-mcp.md) · [Part 2: Abilities](../README.md#part-2-abilities) · [Sub-agents →](04-sub-agents.md)
<!-- /nav:bottom -->
