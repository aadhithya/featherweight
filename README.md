# micro

**An opinionated agent harness. Pydantic is the spec. Everything else follows.**

An agent is a typed class. Its input, output, skills and sub-agents are declared right there and enforced by Pydantic,
not by convention, and not by hoping the model behaves. The boilerplate every agent project ends up writing (retries,
limits, failures, tracing, tools, delegation) is already written. You write the pipeline. That's the part that's yours.

A malformed agent never reaches the model. A running one has a known input and a known output, because both are typed
and both are enforced. No guessing.

It has opinions. Take them, or fork off.

- **Everything is declared.** An agent is a class. So is a skill. Input, output, tools, skills, sub-agents, MCP servers:
  all written on it, all settled before anything runs. You never build the instance. The runner does. That's its job.
- **Pydantic is the contract.** In, out, on skill load, on sub-agent list. Break a rule and you get retried. Nobody gets a
  pass.
- **Failures are values.** A failed run returns `FAILED` and the error. It doesn't throw. Deal with it. Retries, output
  checks, a concurrency limit and `run_many` fan-out come free. (A wrong input type is a bug, and that does raise.)
- **Code checks. The model proposes.** Validators get the last word. Pipelines are plain async Python. Order, loops, when
  to stop: yours. This is not a workflow engine, and it won't pretend to be one.
- **Everything is on record.** Every attempt, in sqlite, tied to the agent that launched it. OpenTelemetry? Too big. (See
  [agents](docs/agents.md#jobs-and-tracing).)

Agents launch agents. MCP servers are named, not wired. Skills use the
[Agent Skills](https://agentskills.io/specification) format. All of it is in the declaration.


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

## Why does this exist? Pydantic AI and LangGraph are right there.

They are. Use them. This is built on LangChain and LangGraph anyway.

- **Boredom.** Every agent project needs the same retries, limits and MCP wiring. I wrote them once and got on with my day.
- **Background agents only.** Pipelines, batches, fan-out. No chat, no UI, no streaming. Nobody is watching the tokens.
- **Very opinionated.** Sorry, not sorry. Don't like them? Fork off.

Want durable execution, evals or OpenTelemetry? Pydantic AI. Want a graph that pauses and resumes? LangGraph. Resuming is
planned here, just not yet. Until then, if a run dies, it starts over.

## Documentation

| | |
|---|---|
| [**Start here**](docs/README.md) | How the docs fit together: guides, specs and examples, and where to begin |
| [Agents](docs/agents.md) | Defining and running an agent, results, retries, checks, tools, jobs and tracing, configuration |
| [Launching agents](docs/launching-agents.md) | The `subagents` allow-list, the two tools, depth and breadth limits, concurrency, tracing |
| [MCP tools](docs/mcp.md) | Declaring servers, the runner's connections, what the model gets, failures |
| [Skills](docs/skills.md) | The skill model, how an agent reads its skills, loading, and which parts of the specification are supported |
| [Specs](docs/specs) | What each part is supposed to do and why: agents, the runner (with launching), jobs and tracing, skills, tools and the store, MCP |
| [Example pipeline](docs/example-pipeline.md) | `doclearn`: two pipelines and five agents built on `micro`, and their limits |

## Try it

```
cp .env.example .env          # set GOOGLE_API_KEY
make build
make example-skills           # an agent with three skills (examples/skills); add ARGS="your request"
make example-launching        # an agent that launches two other agents (examples/launching)
make example-mcp              # an agent that uses GitHub's MCP server (examples/mcp; needs GITHUB_TOKEN)
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
| `make example-mcp` | Run the example agent that uses an MCP server |

```
src/micro/            the harness
examples/launching/   an agent that launches other agents (make example-launching)
examples/skills/      an agent that uses skills, with three skills (make example-skills)
examples/mcp/         an agent that uses GitHub's MCP server (make example-mcp)
examples/doclearn/    an example pipeline: its package, sample documents and tests (make pipeline)
docs/                 documentation
tests/                the harness's tests; tests/e2e uses the real model
```

Issues and pull requests are welcome. Run `make hooks` once: commit messages follow
[Conventional Commits](https://www.conventionalcommits.org) (`feat(runner): ...`, `fix: ...`), and `make check` and
`make test` must pass.

## License

Apache License 2.0. See [LICENSE](LICENSE).
