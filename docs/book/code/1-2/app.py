"""Chapter 1-2: the agent, as a typed class. Nothing is run against a model."""

from typing import ClassVar, Literal

from featherweight import AgentBase, AgentInput, AgentOutput


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


if __name__ == "__main__":
    agent = IncidentSummariser()  # a malformed agent is refused here, before any model is involved
    print(agent.name, agent.version)
    print(list(Summary.model_fields))
