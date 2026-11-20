"""Chapter 1-4: the same run, grouped by a job id, and what the run log recorded."""

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


async def main() -> None:
    report = Report(text=Path("reports/01-checkout-outage.txt").read_text())
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
        print(result.status.value, result.output)
        for row in await db.list_runs(
            job_id=job_id
        ):  # one row for each attempt: metadata, never the report or the summary
            print(
                f"attempt {row.attempt}: {row.status.value}, {row.model_name}, tokens={row.input_tokens}+{row.output_tokens}, "
                f"{row.latency_ms or 0:.0f} ms, input_hash={row.input_hash[:8]}"
            )


if __name__ == "__main__":
    asyncio.run(main())
