"""Chapter 1-5: a rule the schema cannot say, and what a rejected attempt looks like."""

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

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject the attempt
        if output.service and output.service.lower() not in agent_input.text.lower():
            raise ValueError(f"The report does not name the service {output.service!r}")


async def main() -> None:
    report = Report(text=Path("reports/02-sign-in-trouble.txt").read_text())  # this report names no service
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        # the class, not an instance: the runner builds the agent
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
        print(result.status.value, f"attempts={result.attempts}", result.output if result.output else result.error)
        for row in await db.list_attempts(job_id=job_id):
            print(f"attempt {row.attempt}: {row.status.value} {row.error_type or ''}")


if __name__ == "__main__":
    asyncio.run(main())
