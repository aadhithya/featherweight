<!-- nav:top -->
[Book](../README.md) › [Part 2: Abilities](../README.md#part-2-abilities) › MCP
<!-- /nav:top -->

# MCP

Give it the tools of an MCP server.

<!-- code: code/2-2/app.py -->
```python
"""Chapter 2-2: an MCP server. The summariser asks who owns the service."""

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


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "3"
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity or the service is not stated, leave it out and ask in open_questions. "
        "If the report gives a start and an end time, use minutes_between for duration_minutes. "
        "If the report names a service, find who owns it with your tools and put the team in owner."
    )
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
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


async def main(path: str = "reports/03-search-latency.txt") -> None:
    report = Report(text=Path(path).read_text())
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
    print(result.status.value, result.model_name, result.output or result.error)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
```

The server is `services_server.py` in `docs/book/code/`: about 20 lines with `FastMCP` and one tool, `owner_of`. Any MCP server plugs in the same way. Run
the chapter with `make book-chapter N=2-2`. It reads `reports/03-search-latency.txt`, which asks to tell the team that owns search. Something like:

```
SUCCESS gemini-3.5-flash-lite open_questions=[] title='Search was slow' severity='medium' service='search' impact='...' owner='discovery team, on call: Ben' ...
```

**What to notice**

- **`StdioMCPServer` declares a server the runner starts as a subprocess** when a run needs it, and stops afterwards. In the container the
  subprocess runs inside the container too.
- **The model searches for the tool instead of being handed it.** By default it gets `search_tools` and `call_tool`, and the prompt names the
  server and what it is for (`description`). A server with dozens of tools would otherwise put every schema in every call.
  `discover=False` binds the tools directly. See [finding tools by search](../../mcp.md#finding-tools-by-search).
- **The tool's name carries its server:** the model calls `services_owner_of`. Your agent's own `tools` stay as they are.
- **What the server returns is yours.** The run log keeps metadata only, never a tool's result.

> **Why search by default.** Tool schemas stay out of the context until the agent needs them. See [tool discovery](../../specs/tool-discovery.md).

For a real server, `examples/mcp` points an agent at GitHub's MCP server (it needs a token): the declaration is the same with `HttpMCPServer`.
See also [the MCP guide](../../mcp.md).

Next: instructions the agent reads when it needs them.

<!-- nav:bottom -->
---

[← Tools](01-tools.md) · [Part 2: Abilities](../README.md#part-2-abilities) · [Skills →](03-skills.md)
<!-- /nav:bottom -->
