# micro

A tiny wrapper for writing agents, and one way to build a pipeline of them.

Writing agents is still fuzzy: there is no obvious, simple way to define one and know it behaves. `micro` is a small
answer. An agent is a Pydantic class (prompt, model, tools, input and output schemas), so **type checks and the agent's
specification are enforced for free**: Pydantic validates what goes in and what comes out. The runner adds retries,
a concurrency limit, failures returned as results, a trace of every attempt in sqlite, agents that can launch other
agents, and skills.

If you want to build a pipeline of agents, this is one way to start with the boring parts already done. It is about
1,400 lines, built on langchain and langgraph, async throughout, Gemini models for now.

> A small, quick, vibe coded project: a few days of work. A prototype with limited scope, not production software.

## An agent

```python
from typing import ClassVar

from micro.models import AgentBase, AgentInput, AgentOutput


class ExtractInput(AgentInput):
    text: str


class ExtractOutput(AgentOutput):  # also carries `open_questions: list[str]`
    people: list[str] = []


class PeopleExtractor(AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"
    prompt: str = "List every person named in the text."
    input_model: ClassVar[type[AgentInput]] = ExtractInput
    output_model: ClassVar[type[AgentOutput]] = ExtractOutput

    def check_output(self, agent_input, output):  # rules a schema cannot express; raise to reject and retry
        missing = [p for p in output.people if p not in agent_input.text]
        if missing:
            raise ValueError(f"names not in the text: {missing}")
```

```python
import asyncio

from micro.db import Database
from micro.models import RunStatus
from micro.runner import AgentRunner


async def main():
    async with Database("micro.db") as db:
        job_id = await db.create_job("demo")
        result = await AgentRunner(db).run(
            PeopleExtractor, ExtractInput(text="Anna Berger met Tom Okafor on Monday."), job_id=job_id
        )
        if result.status == RunStatus.SUCCESS:
            print(result.output.people)
        else:
            print("failed:", result.error)  # runtime failures are returned, not raised


asyncio.run(main())
```

Set `GOOGLE_API_KEY` first. `runner.run_many(Agent, inputs, job_id=...)` runs one agent over many inputs at once.

## What you get

- **Typed, static agents.** Input, output, tools and prompt are declared; Pydantic enforces them.
- **Failures are results,** not exceptions, so the pipeline decides what a failure means.
- **Retries, output checks and a concurrency limit,** with fan-out over many inputs.
- **Tracing.** Every attempt is a row in sqlite, grouped by job. Pipelines run as jobs. (Why sqlite and not
  OpenTelemetry? To keep it small; see [agents](docs/agents.md#jobs-and-tracing).)
- **Tools** per agent, with a few ready-made ones: exact arithmetic, dates, and a store shared within a job.
- **Agents launching agents.** `subagents = [Researcher, Writer]` is an allow-list; depth and breadth limits; every
  launch traced under its parent.
- **Skills.** Instructions in the [Agent Skills](https://agentskills.io/specification) format: names and descriptions
  in the prompt, the rest read on demand with `get_skill`.

## Documentation

| | |
|---|---|
| [Agents](docs/agents.md) | Defining and running an agent, results, retries, checks, tools, jobs and tracing, configuration |
| [Launching agents](docs/launching-agents.md) | The `subagents` allow-list, the two tools, depth and breadth limits, concurrency, tracing |
| [Skills](docs/skills.md) | The skill model, how an agent reads its skills, loading, and which parts of the specification are supported |
| [Example pipeline](docs/example-pipeline.md) | `doclearn`: two pipelines and five agents built on `micro`, and their limits |

## Try it

```
cp .env.example .env          # set GOOGLE_API_KEY
make build
make example-skills           # an agent with three skills (examples/skills_agent.py); add ARGS="your request"
make pipeline ARGS="examples/project_proposals --category project_proposals"   # the example pipeline
```

## Limits

A prototype, with limited scope on purpose. Not built: evaluation (there is no held-out set or quality metric),
handling of personal data (inputs are kept in plaintext in the run log), and anything beyond Gemini. The harness has the
places a production system would plug in (`AgentRunner(model_factory=...)` for another model, one loop for tracing,
`RunStore` for Redis); a real database, auth, cost accounting and evaluation are work still to do. More in
[the example pipeline's limits](docs/example-pipeline.md#limits).

## Development

| Command | |
|---|---|
| `make build` | Build the image |
| `make test` | Unit tests (the model is mocked, no key needed); or `uv run pytest` |
| `make e2e` | Tests against the real model (costs money) |
| `make pipeline ARGS="..."` | Run the example pipeline |
| `make example-skills` | Run the skills example agent |

```
src/micro/      the harness
src/doclearn/   the example pipeline
examples/       sample documents, skills, and an agent that uses them
docs/           documentation
tests/          mocked tests; tests/e2e uses the real model
```

Issues and pull requests are welcome; run `make test` first.

## License

Apache License 2.0. See [LICENSE](LICENSE).
