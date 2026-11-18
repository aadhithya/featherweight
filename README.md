# micro

**An opinionated agent harness. Pydantic is the spec. Everything else follows.**

An agent is a typed class. Its input, output, skills and sub-agents are declared right there and enforced by Pydantic,
not by convention, and not by hoping the model behaves. The boilerplate every agent project ends up writing (retries,
limits, failures, tracing, tools, delegation) is already written. You write the pipeline. That's the part that's yours.

A malformed agent never reaches the model. A running one has a known input and a known output, because both are typed
and both are enforced. No guessing.

It has opinions. Take them, or fork off.

- **Agents are specs, not scripts.** Static, typed, versioned like any class. Nothing about them is decided at run time.
- **Pydantic is the contract.** Checked on the way in, on the way out, when a skill loads and when a sub-agent list is
  built. Break a rule and you get retried. Nobody gets a pass.
- **Failures are values.** A run that fails returns `FAILED` and the error. It doesn't throw. You decide what that
  means. (A wrong input type is a bug, and it does raise.)
- **Code checks. The model proposes.** Validators have the last word. Pipelines are plain async Python, with no graph
  language to learn.
- **Everything is on record.** Every attempt, in sqlite, tied to the agent that launched it.

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
make example-launching        # an agent that launches two other agents (examples/launching)
make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"   # an example pipeline (examples/doclearn)
```

## Development

| Command | |
|---|---|
| `make build` | Build the image |
| `make test` | Unit tests (the model is mocked, no key needed); or `uv run pytest` |
| `make check` | Lint and format check (ruff) and types (ty); `make lint`, `make format`, `make typecheck` run one |
| `make hooks` | Install the git hooks: conventional commit messages, ruff and ty |
| `make e2e` | Tests against the real model (costs money) |
| `make pipeline ARGS="..."` | Run the example pipeline |
| `make example-skills` | Run the skills example agent |
| `make example-launching` | Run the example agent that launches agents |

```
src/micro/            the harness
examples/launching/   an agent that launches other agents (make example-launching)
examples/skills/      an agent that uses skills, with three skills (make example-skills)
examples/doclearn/    an example pipeline: its package, sample documents and tests (make pipeline)
docs/                 documentation
tests/                the harness's tests; tests/e2e uses the real model
```

Issues and pull requests are welcome. Run `make hooks` once: commit messages follow
[Conventional Commits](https://www.conventionalcommits.org) (`feat(runner): ...`, `fix: ...`), and `make check` and
`make test` must pass.

## License

Apache License 2.0. See [LICENSE](LICENSE).
