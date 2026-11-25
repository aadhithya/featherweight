# An agent that starts another and goes on

Start a child in the background and carry on, without waiting for it. The researcher below starts a web search and keeps working; the child's result is added to its conversation when it is ready.

```python
import asyncio
from typing import ClassVar

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    default_db_path,
)


class Query(AgentInput):
    query: str


class Findings(AgentOutput):
    findings: str = ""


class Question(AgentInput):
    question: str


class Report(AgentOutput):
    report: str = ""


class WebSearcher(AgentBase):
    name: str = "web_searcher"
    description: str = "Searches the web for a query"  # the researcher's model reads this
    version: str = "0.1.0"
    prompt: str = "Search the web for the query and report what you find."
    model: ModelSettings = Gemini()
    input_model: ClassVar[type[AgentInput]] = Query
    output_model: ClassVar[type[AgentOutput]] = Findings


class Researcher(AgentBase):
    name: str = "researcher"
    description: str = "Answers a question, searching the web while it thinks"
    version: str = "0.1.0"
    prompt: str = "Answer the question. Start a web search in the background, and go on while it runs."
    model: ModelSettings = Gemini()
    background_jobs: list[type] = [WebSearcher]  # the model gets `start_background`; the child is a job
    input_model: ClassVar[type[AgentInput]] = Question
    output_model: ClassVar[type[AgentOutput]] = Report


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:  # here, in this process
        job_id = await db.create_job("research")
        result = await runner.run(Researcher, Question(question="Who built the first lighthouse?"), job_id=job_id)
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

To run the child on a worker, give the runner a client: `AgentRunner(db, background=client)`. A chat turn does not wait: follow `background_started` and `background_finished` on its stream, or the child's job events, and start the next turn yourself. `await runner.background_message(job_id)` is a user message with the child's result, for that next turn. [The background jobs guide](../background-jobs.md) has the rest.
