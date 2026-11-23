"""Chapter 5-5: who is asking. A tool that checks the identity of the run before it acts."""

import asyncio
import sys
from typing import ClassVar

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel

from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, RunContext, SqliteDatabase

INCIDENTS = {"INC-7": ("payments", "Checkout is down for card payments."), "INC-9": ("search", "Search is slow.")}


class Responder(BaseModel):  # who the run is for: you define it, small, and no secrets
    user_id: str
    team: str


@tool
async def open_incident(incident_id: str, runtime: ToolRuntime[RunContext[Responder]]) -> str:
    """Open an incident by its id."""
    who = runtime.context.identity  # the model never sees this parameter, and cannot change it
    team, summary = INCIDENTS.get(incident_id, ("", ""))
    if who is None or team != who.team:  # featherweight carries the identity; checking it is the tool's job
        return "Not allowed."
    return summary


class Question(AgentInput):
    text: str


class Answer(AgentOutput):
    answer: str | None = None


class IncidentLookup(AgentBase):
    name: str = "incident_lookup"
    description: str = "Answers a question from the incidents the asker may open"
    version: str = "1"
    prompt: str = "Answer from the incidents you can open with your tool. If you cannot open one, say so."
    tools: list = [open_incident]
    input_model: ClassVar[type[AgentInput]] = Question
    output_model: ClassVar[type[AgentOutput]] = Answer


async def main(user_id: str = "ana", team: str = "payments") -> None:
    async with SqliteDatabase(":memory:") as db:
        runner = AgentRunner(db, identity_type=Responder)  # a declared type makes identity= required on every run
        result = await runner.run(
            IncidentLookup,
            Question(text="What is going on with INC-7?"),
            identity=Responder(user_id=user_id, team=team),
        )
        print(result.output.answer if result.output else result.error)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
