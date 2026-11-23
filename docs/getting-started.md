# Getting started

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

Install it, write an agent, run it. For a guided path that builds one small project from a first agent to workers, read [the book](book/README.md).

## Install

```
pip install featherweight
pip install 'featherweight[anthropic]'    # or mcp, openai, postgres, otel
pip install 'featherweight[all]'          # all of them
```

Python 3.12 or later. Import what you use: `from featherweight import AgentBase, AgentRunner`. (`import featherweight as ftw`
works too, as a joke.)

## With a coding agent

There is a skill that teaches a coding agent to build with featherweight. In Claude Code:

```
/plugin marketplace add aadhithya/featherweight
/plugin install featherweight@featherweight
```

Other tools that read Agent Skills: copy [`skills/featherweight`](../skills/featherweight) into their skills folder.

## An agent

An agent is a class. Its input and output are Pydantic models, and `check_output` checks what a schema cannot.

```python
from typing import ClassVar

from featherweight import AgentBase, AgentInput, AgentOutput


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
            raise ValueError(f"Not in the text: {missing}")
```

A malformed agent is refused when the class is built, so it never reaches the model.

## Run it

```python
import asyncio

from featherweight import AgentRunner, Status, SqliteDatabase


async def main():
    async with SqliteDatabase("featherweight.db") as db:
        job_id = await db.create_job("demo")
        result = await AgentRunner(db).run(
            PeopleExtractor, ExtractInput(text="Anna Berger met Tom Okafor on Monday."), job_id=job_id
        )
        if result.status == Status.SUCCESS:
            print(result.output.people)
        else:
            print(result.error)  # a failed run is a value, not an exception


asyncio.run(main())
```

Set `GOOGLE_API_KEY` first. The default model is Gemini, and the [other providers](models.md) take their own key.
`runner.run_many(Agent, inputs, job_id=...)` runs one agent over many inputs at once.

The run log is a sqlite file here. For a deployment, `PostgresDatabase` ships in the package (`pip install 'featherweight[postgres]'`), and switching is one line: see [persistence](persistence.md).

The `job_id` is the job the run belongs to; each run has a `run_id` and each of its attempts an `attempt_id` ([agents](agents.md#running-an-agent)).

## Where next

- [Agents](agents.md): results, retries, tools, jobs and tracing, resuming a job.
- [Launching agents](launching-agents.md), [MCP tools](mcp.md), [skills](skills.md), [models](models.md).
- [Streaming agents](streaming.md) for chat, and [submitted jobs](submitted-jobs.md) for work that runs in the background.

## Try the examples

```
cp .env.example .env          # set GOOGLE_API_KEY (the other keys only for Claude or OpenAI agents)
make build
make example-skills           # an agent with three skills (examples/skills); add ARGS="your request"
make example-launching        # an agent that launches two other agents (examples/launching)
make example-mcp              # an agent that uses GitHub's MCP server (examples/mcp; needs GITHUB_TOKEN)
make example-resume           # a pipeline that crashes halfway and resumes (examples/resume; Claude Haiku, needs ANTHROPIC_API_KEY)
make example-jobs             # submit a job and run it in a worker (examples/jobs; no model, no key)
make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"   # an example pipeline (examples/doclearn)
```

Each example has a README that says what to look at; they are listed in [`examples/`](../examples/README.md).
