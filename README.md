# Featherweight

**An opinionated agent harness. Pydantic is the spec. Everything else follows.**


An agent is a typed class. Its input, output, skills and sub-agents are declared right there and enforced by Pydantic,
not by convention, and not by hoping the model behaves. The boilerplate every agent project ends up writing (retries,
limits, failures, tracing, tools, delegation) is already written. You write the pipeline. That's the part that's yours.

A malformed agent never reaches the model. A running one has a known input and a known output, because both are typed
and both are enforced. No guessing.

It has opinions. Take them, or fork off.

- **Separation of concerns.** Agents are classes. The runner runs them. Your code decides what happens next. Nobody gets
  to be clever.
- **Typed contracts.** Pydantic checks what goes in and what comes out, and `check_output` checks what a schema can't.
  Break the contract and the attempt fails.
- **Failures are values.** A failed run returns `FAILED` and the error. It doesn't throw. Retries, a concurrency limit and
  `run_many` fan-out come free.
- **Plain Python pipelines.** Order, loops and branching are `if` and `for`. No special workflow syntax, no nodes and
  edges to wire up. This is not a workflow engine, and it won't pretend to be one.
- **Delegation on a leash.** Agents can launch other agents, but only the ones on their list, and only so deep and so
  wide. A runaway recursion dies.
- **Everything is on record.** Every attempt goes in sqlite: tokens, errors, which agent launched which. OpenTelemetry?
  Too big. (See [agents](docs/agents.md#jobs-and-tracing).)
- **Talks while it works.** A streaming agent streams its answer as [AG-UI](docs/streaming.md) events, with no output to
  validate and no retry, for chat. The Vercel AI SDK works too, and the user can stop it midway and correct it.
- **Plug things in.** The model (Gemini, Gemma, Claude, OpenAI, or your own subclass), MCP servers and skills are
  declared on the agent. Tool schemas and skill instructions stay out of the context until the agent needs them.

> Designed by a human. Implemented by Claude.


## An agent

```python
from typing import ClassVar

import featherweight as ftw


class ExtractInput(ftw.AgentInput):
    text: str


class ExtractOutput(ftw.AgentOutput):  # also carries `open_questions: list[str]`
    people: list[str] = []


class PeopleExtractor(ftw.AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"
    prompt: str = "List every person named in the text."
    input_model: ClassVar[type[ftw.AgentInput]] = ExtractInput
    output_model: ClassVar[type[ftw.AgentOutput]] = ExtractOutput

    def check_output(self, agent_input, output):  # rules a schema cannot express; raise to reject and retry
        missing = [p for p in output.people if p not in agent_input.text]
        if missing:
            raise ValueError(f"names not in the text: {missing}")
```

```python
import asyncio

import featherweight as ftw


async def main():
    async with ftw.Database("featherweight.db") as db:
        job_id = await db.create_job("demo")
        result = await ftw.AgentRunner(db).run(
            PeopleExtractor, ExtractInput(text="Anna Berger met Tom Okafor on Monday."), job_id=job_id
        )
        if result.status == ftw.RunStatus.SUCCESS:
            print(result.output.people)
        else:
            print("failed:", result.error)  # runtime failures are returned, not raised


asyncio.run(main())
```

Set `GOOGLE_API_KEY` first. The default model is Gemini, and the [other providers](docs/models.md) take their own key.
`runner.run_many(Agent, inputs, job_id=...)` runs one agent over many inputs at once.

## Why does this exist? Pydantic AI and LangGraph are right there.

They are. Use them. This is built on LangChain and LangGraph anyway.

- **Boredom.** Every agent project needs the same retries, limits and MCP wiring. I wrote them once and got on with my day.
- **Mostly background agents.** Pipelines, batches, fan-out: nobody is watching the tokens. A chat agent streams its answer
  ([streaming agents](docs/streaming.md)), but there is no UI, and no stored conversations.
- **Very opinionated.** Sorry, not sorry. Don't like them? Fork off.

Want durable execution, evals or OpenTelemetry? Pydantic AI. Want a graph that pauses and resumes? LangGraph. Here, a failed job is run again with
`run_job(..., resume_from=old_job_id)`: your pipeline code runs again, and the agent runs that already succeeded are
answered from the run log ([resuming a job](docs/agents.md#resuming-a-job)).

## Build with a coding agent

There is a skill that teaches a coding agent to build with `featherweight`. In Claude Code:

```
/plugin marketplace add aadhithya/featherweight
/plugin install featherweight@featherweight
```

Other tools that read Agent Skills: copy [`skills/featherweight`](skills/featherweight) into their skills folder.

## Documentation

- [**Docs**](docs/README.md): guides for using it, and where to begin
- [**Specs**](docs/specs/README.md): what each part does, and why
- [**Examples**](examples/README.md): code you can run

## Try it

```
cp .env.example .env          # set GOOGLE_API_KEY (the other keys only for Claude or OpenAI agents)
make build
make example-skills           # an agent with three skills (examples/skills); add ARGS="your request"
make example-launching        # an agent that launches two other agents (examples/launching)
make example-mcp              # an agent that uses GitHub's MCP server (examples/mcp; needs GITHUB_TOKEN)
make example-resume           # a pipeline that crashes halfway and resumes (examples/resume; Claude Haiku, needs ANTHROPIC_API_KEY)
make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"   # an example pipeline (examples/doclearn)
```

## Development

| Command | |
|---|---|
| `make build` | Build the image |
| `make test` | Unit tests (the model is mocked, no key needed); or `uv run pytest` |
| `make check` | Lint and format check (ruff) and types (ty); `make lint`, `make format`, `make typecheck` run one |
| `make hooks` | Install the git hooks: conventional commit messages, ruff, ty and the unit tests. CI only lints and type checks, so the tests run here, on commit |
| `make e2e` | Tests against the real model (costs money) |
| `make pipeline ARGS="..."` | Run the example pipeline |
| `make example-skills` | Run the skills example agent |
| `make example-launching` | Run the example agent that launches agents |
| `make example-mcp` | Run the example agent that uses an MCP server |
| `make example-resume` | Run the example that resumes a failed job |

```
src/featherweight/            the harness
examples/launching/   an agent that launches other agents (make example-launching)
examples/skills/      an agent that uses skills, with three skills (make example-skills)
examples/mcp/         an agent that uses GitHub's MCP server (make example-mcp)
examples/resume/      a pipeline that crashes halfway and resumes (make example-resume)
examples/doclearn/    an example pipeline: its package and sample documents (make pipeline)
docs/                 documentation
tests/                the harness's tests; tests/e2e uses the real model
```

Issues and pull requests are welcome. How a release is made: [docs/releasing.md](docs/releasing.md). Run `make hooks` once: commit messages follow
[Conventional Commits](https://www.conventionalcommits.org) (`feat(runner): ...`, `fix: ...`), and `make check` and
`make test` must pass.

## License

Apache License 2.0. See [LICENSE](LICENSE).
