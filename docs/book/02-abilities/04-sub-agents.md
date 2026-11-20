<!-- nav:top -->
[Book](../README.md) › [Part 2: Abilities](../README.md#part-2-abilities) › Sub-agents
<!-- /nav:top -->

# Sub-agents

Let it hand part of its work to another agent.

The skill from chapter 2-3 stays where it is: this chapter's code still reads `2-3/severity-rubric`.

<!-- code: code/2-4/app.py -->
```python
"""Chapter 2-4: a sub-agent. The summariser launches a planner when the report gives no follow-up."""

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


class Report(AgentInput):
    text: str


class Summary(AgentOutput):  # also carries `open_questions: list[str]`
    title: str = ""
    severity: Literal["low", "medium", "high"] | None = None
    service: str | None = None
    impact: str = ""
    duration_minutes: int | None = None
    owner: str | None = None  # new: the answer of the MCP server
    next_steps: list[str] = []


class Impact(AgentInput):
    impact: str


class Plan(AgentOutput):
    steps: list[str] = []


class ActionPlanner(AgentBase):  # the sub-agent: an agent like any other
    name: str = "action_planner"
    description: str = "Proposes two or three next steps for an incident, from its impact"
    version: str = "1"
    prompt: str = "Propose two or three concrete next steps for the incident."
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    input_model: ClassVar[type[AgentInput]] = Impact
    output_model: ClassVar[type[AgentOutput]] = Plan


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "5"
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity is not stated, decide it with your skill. If the service is not stated, leave it out and ask in open_questions. "
        "If the report gives a start and an end time, use minutes_between for duration_minutes. "
        "If the report names a service, find who owns it with your tools and put the team in owner. "
        "If the report lists no follow-up, launch the action_planner with the impact and use its steps as next_steps."
    )
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    skills: list[Skill] = [Skill.from_directory("2-3/severity-rubric")]  # read on demand, not in every prompt
    subagents: list[type] = [ActionPlanner]  # the only agent this one may launch
    tools: list[BaseTool] = [minutes_between]
    mcp_servers: list[StdioMCPServer | HttpMCPServer] = [  # the runner starts this server when the agent needs it
        StdioMCPServer(
            name="services",
            command=sys.executable,
            args=("services_server.py",),
            description="Who owns a service, and who is on call",
        )
    ]
    input_model: ClassVar[type[AgentInput]] = Report
    output_model: ClassVar[type[AgentOutput]] = Summary

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject the attempt
        if output.service and output.service.lower() not in agent_input.text.lower():
            raise ValueError(f"The report does not name the service {output.service!r}")


async def main(path: str = "reports/02-sign-in-trouble.txt") -> None:
    report = Report(text=Path(path).read_text())
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
    print(result.status.value, result.model_name, result.output or result.error)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
```

Run it with `make book-chapter N=2-4`. It reads `reports/02-sign-in-trouble.txt`, which lists no follow-up. Something like:

```
SUCCESS gemini-3.5-flash-lite open_questions=[...] title='Sign-in trouble' severity='medium' ... next_steps=['Find the cause in the server logs', 'Add an alert on blank sign-in pages']
```

**What to notice**

- **`ActionPlanner` is an ordinary agent,** with its own input and output models. Nothing marks it as a sub-agent.
- **`subagents` is an allow-list of classes.** The agent can launch exactly these. The prompt lists them, and the runner gives the agent two
  tools: `get_agent_schema` and `launch_agent`. `launch_agent` waits for the child and returns its output as text. See
  [launching agents](../../launching-agents.md).
- **The child is a run of its own,** with its own rows in the run log, linked to the parent's.
- **Limits keep a chain from running away:** how deep agents may launch agents, and how many launches one attempt may make. They are
  [in the guide](../../launching-agents.md#limits); nothing to set for this example.
- **A launched child blocks its parent.** For work that goes on while the agent carries on, see [background agents](../05-in-production/03-background-agents.md).

> **Why an allow-list of classes.** There is no registry to maintain, and an agent can launch only what it declares. See
> [launching agents](../../specs/runner.md#launching-agents).

Next: Part 3 adds jobs to the assistant, to run it over many reports.

<!-- nav:bottom -->
---

[← Skills](03-skills.md) · [Part 2: Abilities](../README.md#part-2-abilities) · [What a job is →](../03-jobs-and-pipelines/01-what-a-job-is.md)
<!-- /nav:bottom -->
