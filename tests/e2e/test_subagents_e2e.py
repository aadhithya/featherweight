"""End-to-end test of an agent that launches an agent, with the real model: the parent has to ask for the child's input
schema, launch it with the input as a JSON string and use what comes back. About four model calls. Run with
`make e2e ARGS="-k subagents"` (needs GOOGLE_API_KEY in .env). Not part of `make test`.
"""

from typing import ClassVar

from micro.models import AgentBase, AgentInput, AgentOutput, RunStatus
from micro.runner import AgentRunner

ARTICLE = (
    "The harbour authority has approved a new cold-storage warehouse at Pier 4. Construction starts in March and "
    "should take fourteen months. The warehouse will hold 6,000 pallets of fresh produce."
)


class SummariserInput(AgentInput):
    text: str
    max_words: int


class SummariserOutput(AgentOutput):
    summary: str | None = None


class Summariser(AgentBase):
    name: str = "summariser"
    description: str = "Summarises a text in at most the given number of words"
    version: str = "0.1.0"
    prompt: str = "Summarise the text in at most `max_words` words. Keep the figures."
    model_name: str = "gemini-3.8-flash"
    reasoning: str = "low"
    input_model: ClassVar[type[AgentInput]] = SummariserInput
    output_model: ClassVar[type[AgentOutput]] = SummariserOutput


class ReporterInput(AgentInput):
    article: str


class ReporterOutput(AgentOutput):
    headline: str | None = None
    summary: str | None = None


class Reporter(AgentBase):
    name: str = "reporter"
    description: str = "Writes a headline and a short summary for an article"
    version: str = "0.1.0"
    prompt: str = (
        "Write a headline and a summary for the article. Get the summary from the summariser agent (at most 20 words), "
        "and return it unchanged as `summary`. Write the headline yourself."
    )
    model_name: str = "gemini-3.8-flash"
    reasoning: str = "low"
    subagents: list[type] = [Summariser]
    input_model: ClassVar[type[AgentInput]] = ReporterInput
    output_model: ClassVar[type[AgentOutput]] = ReporterOutput


async def test_an_agent_launches_another_agent_with_the_real_model(db):
    job_id = await db.create_job("e2e-subagents")

    result = await AgentRunner(db).run(Reporter, ReporterInput(article=ARTICLE), job_id=job_id)

    assert result.status == RunStatus.SUCCESS, result.error
    print(f"\n[reporter] {result.output.headline!r}\n[summary] {result.output.summary!r}")
    rows = await db.list_runs(job_id=job_id)
    print("[rows]", [(r.agent_name, r.depth, r.status.value, r.input_tokens, r.output_tokens) for r in rows])
    parent = next(r for r in rows if r.agent_name == "reporter")
    children = [r for r in rows if r.agent_name == "summariser" and r.status == RunStatus.SUCCESS]
    assert children, "the reporter never launched the summariser"
    assert children[0].parent_agent_run_id == parent.agent_run_id and children[0].depth == 2
    assert result.output.summary and result.output.headline
    assert children[0].output["summary"].strip() == result.output.summary.strip(), "the summary was not the child's"
