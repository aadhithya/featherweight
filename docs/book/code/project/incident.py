"""The assistant: it summarises an incident report, with a tool, an MCP server, a skill and a sub-agent (Parts 1 and 2)."""

import sys
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal

from langchain_core.tools import BaseTool, tool

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    Gemini,
    HttpMCPServer,
    ModelSettings,
    Skill,
    StdioMCPServer,
)

HERE = Path(__file__).parent
# The model is a setting of the agent. Providers: Gemini (the default), Gemma, Claude, OpenAI, or your own (Chapter 1-6).
MODEL = Gemini(model="gemini-3.5-flash-lite", thinking="low")


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
    owner: str | None = None
    next_steps: list[str] = []


class Impact(AgentInput):
    impact: str


class Plan(AgentOutput):
    steps: list[str] = []


class ActionPlanner(AgentBase):  # the sub-agent
    name: str = "action_planner"
    description: str = "Proposes two or three next steps for an incident, from its impact"
    version: str = "1"
    prompt: str = "Propose two or three concrete next steps for the incident."
    model: ModelSettings = MODEL
    input_model: ClassVar[type[AgentInput]] = Impact
    output_model: ClassVar[type[AgentOutput]] = Plan


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "6"
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity is not stated, decide it with your skill. If the service is not stated, leave it out and ask in open_questions. "
        "If the report gives a start and an end time, use minutes_between for duration_minutes. "
        "If the report names a service, find who owns it with your tools and put the team in owner. "
        "If the report lists no follow-up, launch the action_planner with the impact and use its steps as next_steps."
    )
    model: ModelSettings = MODEL
    skills: list[Skill] = [
        Skill.from_directory(HERE / "severity-rubric")
    ]  # read on demand, so it is not in every prompt
    subagents: list[type] = [ActionPlanner]  # the only agent this one may launch, and it waits for the answer
    tools: list[BaseTool] = [minutes_between]  # a plain function: the model is bad at date arithmetic
    # Two kinds of MCP server: stdio (the runner starts it as a subprocess, as here) and HTTP (a server that is already running).
    mcp_servers: list[StdioMCPServer | HttpMCPServer] = [
        StdioMCPServer(
            name="services",
            command=sys.executable,
            args=(str(HERE.parent / "services_server.py"),),
            description="Who owns a service, and who is on call",
        )
    ]
    input_model: ClassVar[type[AgentInput]] = Report
    output_model: ClassVar[type[AgentOutput]] = Summary

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject the attempt
        if output.service and output.service.lower() not in agent_input.text.lower():
            raise ValueError(f"The report does not name the service {output.service!r}")
