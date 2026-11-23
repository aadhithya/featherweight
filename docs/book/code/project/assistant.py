"""The chat (Part 4), with a researcher that it can start in the background (Part 5)."""

from typing import ClassVar

from incident import MODEL, Report

from featherweight import AgentBase, AgentInput, AgentOutput, ModelSettings, StreamingAgent


class RootCause(AgentOutput):
    cause: str = ""


class RootCauseResearcher(AgentBase):  # an ordinary agent: the chat starts it in the background
    name: str = "root_cause_researcher"
    description: str = "Reads an incident report and names the most likely root cause"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "Name the most likely root cause of the incident in one or two sentences. Use only what the report says."
    )
    model: ModelSettings = MODEL
    input_model: ClassVar[type[AgentInput]] = Report  # the contract is set on the class: the same for every run
    output_model: ClassVar[type[AgentOutput]] = RootCause


class IncidentAssistant(StreamingAgent):  # a streaming agent answers in a chat: no output model, no retries
    name: str = "incident_assistant"
    description: str = "Answers questions about an incident report"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "The first message is an incident report. Answer questions about it in a sentence or two. "
        "When the user asks for the root cause, start the researcher in the background with the report text and carry on: "
        "its note arrives in a later message, and then you tell the user what it says."
    )
    model: ModelSettings = MODEL
    background_jobs: list[type] = [RootCauseResearcher]  # not `subagents`: the chat goes on, and does not wait for it
