# micro

**An opinionated agent harness, where Pydantic is the specification.**

An agent is a typed Pydantic class, and everything about how it behaves is declared in that class and enforced by
Pydantic, not left to convention or to the model: its input, its output, its skills and the agents it may launch. The
runner then makes it dependable: failures come back as values, retries and limits are built in, and every attempt is
recorded.

It is also a convenience. Every agent project ends up writing the same boilerplate: validating inputs and outputs,
retrying, limiting concurrency, handling failures, tracing runs, wiring up tools, letting one agent call another, loading
instructions for an agent to follow. `micro` is that boilerplate, written once, so that you can start from a working base
and spend your time on the pipeline: the part that is your product.

The opinions, which are the whole design:

- **An agent is a specification, not a script.** It is a static, typed definition: name, version, prompt, model,
  tools, and input and output schemas. Nothing about it is decided at run time, so it can be read, tested and versioned
  like any other class.
- **Pydantic enforces the contract at every boundary.** The input before the model is called, the output after, a skill's
  fields when it is loaded, the list of sub-agents when the agent is built. A model that breaks a rule gets retried; it
  does not get to pass.
- **Failures are values.** A run that fails at run time returns a result, `FAILED` with the error, not an exception, so
  the pipeline decides what a failure means. (Programming errors, such as the wrong input type, do raise.)
- **Code checks, the model proposes.** Validators and `check_output` hold what a schema cannot, and a pipeline is
  ordinary async Python around `run` and `run_many`, with no graph language to learn.
- **Every attempt is on record.** Inputs, outputs, errors, tokens, latency and the prompt's hash, in sqlite, grouped by
  job and tied to the agent that launched it.
- **Small enough to read.** About 1,400 lines on langchain and langgraph, async throughout, Gemini models for now. You
  should be able to read all of it, and change the parts you disagree with.

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

- **Typed, static agents,** with their input and output enforced by Pydantic.
- **Retries, output checks and a concurrency limit,** with fan-out over many inputs. Failures are returned, not raised.
- **Tracing.** Every attempt is a row in sqlite, grouped by job. Pipelines run as jobs. (Why sqlite and not
  OpenTelemetry? To keep it small; see [agents](docs/agents.md#jobs-and-tracing).)
- **Tools** per agent, with a few ready-made ones: exact arithmetic, dates, and a store shared within a job.
- **Agents launching agents.** `subagents = [Researcher, Writer]` is an allow-list of classes; depth and breadth limits;
  every launch traced under its parent.
- **Skills.** Instructions in the [Agent Skills](https://agentskills.io/specification) format, as a Pydantic model that
  checks the specification's rules: names and descriptions in the prompt, the rest read on demand with `get_skill`.

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
make example-skills           # an agent with three skills (examples/skills); add ARGS="your request"
make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"   # an example pipeline (examples/doclearn)
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
src/micro/            the harness
examples/skills/      an agent that uses skills, with three skills (make example-skills)
examples/doclearn/    an example pipeline: its package, sample documents and tests (make pipeline)
docs/                 documentation
tests/                the harness's tests; tests/e2e uses the real model
```

Issues and pull requests are welcome; run `make test` first.

## License

Apache License 2.0. See [LICENSE](LICENSE).
