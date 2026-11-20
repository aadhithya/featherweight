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
    version: str = "4"
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity is not stated, decide it with your skill. If the service is not stated, leave it out and ask in open_questions. "
        "If the report gives a start and an end time, use minutes_between for duration_minutes. "
        "If the report names a service, find who owns it with your tools and put the team in owner."
    )
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    skills: list[Skill] = [Skill.from_directory("2-3/severity-rubric")]  # read on demand, not in every prompt
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
