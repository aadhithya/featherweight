---
name: featherweight
description: "Build background AI agents and pipelines with the Featherweight Python framework: typed Pydantic agents, a runner with retries and concurrency limits, sub-agents, MCP tools, Agent Skills, and a sqlite trace. Use when the user wants to build, extend, test or debug an app, pipeline, batch job or agent system with Featherweight, or when code imports `featherweight.models`, `featherweight.runner` or `featherweight.model_settings`."
---

# Building with Featherweight

Featherweight is a small Python framework for **background agents**: pipelines, batches, fan-out. A chat agent that streams its
answer is a `StreamingAgent`; there is no UI and no stored conversation.
An agent is a typed class. A runner runs it with retries, limits and a trace. Your code, plain async Python, decides what
runs next. It has opinions, but they are defaults: follow them unless the user wants something else.

Install (Python 3.12 or later). Add the extras the app needs: `mcp` for MCP servers, `anthropic` for Claude, `openai` for
OpenAI. Gemini and Gemma need no extra.

```
pip install featherweight
pip install "featherweight[mcp,anthropic,openai]"   # with all the extras
```

Gemini is the default model and needs `GOOGLE_API_KEY`. Claude needs `ANTHROPIC_API_KEY` and OpenAI `OPENAI_API_KEY`.

## The model

Three layers, and each owns one thing:

| Layer | Owns | Is |
|---|---|---|
| **The definition** | What an agent is | A subclass of `AgentBase`. Static. Checked when it is built |
| **The runner** | How it runs | `AgentRunner`: attempts, retries, concurrency, delegation, MCP connections, the trace |
| **Your code** | What happens next | The pipeline: order, loops, branching, when to stop. Plain async Python |

Do not put orchestration in an agent, and do not build a graph. A pipeline is `await runner.run(...)`, `if` and `for`.

## A working app

Define the agent: input, output, prompt.

```python
from typing import ClassVar

import featherweight as ftw


class ExtractInput(ftw.AgentInput):
    text: str


class ExtractOutput(ftw.AgentOutput):  # also carries `open_questions: list[str]`
    people: list[str] = []  # give task fields defaults, so a model that must ask a question is not forced to invent


class PeopleExtractor(ftw.AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"  # change it when the prompt or behaviour changes
    prompt: str = "List every person named in the text."
    model: ftw.Gemini = ftw.Gemini(model="gemini-3.5-flash-lite", thinking="low")
    input_model: ClassVar[type[ftw.AgentInput]] = ExtractInput
    output_model: ClassVar[type[ftw.AgentOutput]] = ExtractOutput

    def check_output(self, agent_input, output):  # rules a schema cannot express; raise to reject and retry
        missing = [p for p in output.people if p not in agent_input.text]
        if missing:
            raise ValueError(f"names not in the text: {missing}")
```

Run it from a pipeline:

```python
import asyncio

import featherweight as ftw


async def main():
    async with ftw.SqliteDatabase("featherweight.db") as db:
        job_id = await db.create_job("demo")  # runs of one pipeline share a job
        runner = ftw.AgentRunner(db)
        result = await runner.run(PeopleExtractor, ExtractInput(text="Anna Berger met Tom Okafor."), job_id=job_id)
        if result.status == ftw.RunStatus.SUCCESS:
            print(result.output.people)
        else:
            print("failed:", result.error)  # a runtime failure is returned, not raised


asyncio.run(main())
```

`runner.run_many(Agent, inputs, job_id=...)` runs one agent over many inputs at once and returns results in input order.

## Rules that save you a round trip

- **Pass the class to the runner, not an instance.** `runner.run(PeopleExtractor, input)`. The runner builds it.
- **Check `result.status`.** It is a `RunStatus`, a string enum: `RunStatus.SUCCESS` or `RunStatus.FAILED`. A failed run
  returns `FAILED` and `result.error`. It does not raise. A wrong input type does raise, and that is a bug in your code.
  `run_job` returns a `JobOutcome` with `job_id`, `status`, `result` (what your pipeline returned) and `error`.
- **Two kinds of bad output.** A reply that breaks the schema (a wrong type, a value outside a `Literal`) goes back to the
  model within the same attempt. A rejection by `check_output` fails the attempt, and the runner retries it, up to 3
  attempts. Use a `Literal` or validator for rules on the output alone, `check_output` for rules about the input too.
- **Resuming a failed job:** `run_job(db, name, pipeline, resume_from=old_job_id)` makes a new job. Agent runs that
  already succeeded with the same agent and input are reused (`result.reused`, zero tokens); the rest run. The code
  between agents must be deterministic, the run store is not used for resuming, and only top-level runs are reused
  (see `references/agents.md`).
- **A retry resamples with the same input.** The error is not sent back. If the retry must know what went wrong, build a
  new input in the pipeline that carries the problems.
- **Output models:** give task fields defaults, and prefer flat lists with ids over nested or `X | None` objects. Gemini
  can truncate a nested schema without an error, so add a `check_output` that compares the output with the input.
- **Models are typed classes:** `Gemini`, `Gemma`, `Claude`, `OpenAI` from `featherweight.model_settings`. Each checks its models
  and efforts when the agent is built. **There is no `temperature`, `top_p` or `top_k`**: the current models refuse or
  ignore them, and the classes reject them. For other models or providers, subclass (see `references/models.md`).
- **Tools** are langchain `@tool` functions in `tools=[...]`. The runner adds its own tools (`launch_agent`, `get_skill`,
  `search_tools`, `call_tool`, ...), and an agent's own tool cannot use those names.
- **MCP servers** are declared, not wired: `mcp_servers=[HttpMCPServer(...)]` or `StdioMCPServer(...)` (needs `featherweight[mcp]`).
  Secrets are the **names** of environment variables (`bearer_from="GITHUB_TOKEN"`), never values. Tools are found by
  search by default; `discover=False` binds a small server's tools directly.
- **Sub-agents:** `subagents=[OtherAgent]`, classes on an allow-list. Depth and breadth are limited (3 and 10).
- **Skills:** `skills=[Skill.from_directory(path)]`, Agent Skills format. The agent reads them when it decides to.
- **Check what the model returned in code.** Validators and `check_output` have the last word. The model proposes.

## Tests and cost

- **Never call a real model in tests.** A fake is model settings: a `ModelSettings` subclass whose `build()` returns a fake
  chat model, put on a subclass of the agent under test. `references/recipes.md` has a whole test file, with a fake that
  works with `run_many`. Tests need `pytest-asyncio` with `asyncio_mode = auto`.
- Real calls cost money. Ask the user before running one, use the smallest model (`gemini-3.5-flash-lite`,
  `claude-haiku-5-5`, `gpt-6-luna`) at the lowest effort, and keep the input small.

## Where to read next

Read only the reference for the task. Each is a guide from the Featherweight repository, with its code examples.

| The task | Read |
|---|---|
| Define and run an agent, results, retries, checks, tools, jobs, tracing, configuration | `references/agents.md` |
| Patterns: a pipeline, feeding errors back, a fake model for tests, a second agent | `references/recipes.md` |
| Pick a model or provider, effort, add GPT-5.x or Ollama | `references/models.md` |
| An agent that launches other agents | `references/launching-agents.md` |
| Use an MCP server, discovery, secrets, failures | `references/mcp.md` |
| Give an agent skills | `references/skills.md` |
| A chat agent that streams its answer: `StreamingAgent`, `runner.stream()`, AG-UI and Vercel adapters, stopping, keeping the conversation | `references/streaming.md` |
| What is kept and what is not, resuming a job (`resumable=True`), a result store of your own, choosing a database | `references/persistence.md` |
| Submit a job to a queue and run it in a worker: `JobClient`, `JobWorker`, `JobQueue`, cancelling, events and SSE | `references/submitted-jobs.md` |
| See a whole pipeline of several agents | `references/example-pipeline.md` |

The full specs, which say what each part does and why, are in `docs/specs` of the repository, and runnable examples are in
`examples/` (`launching`, `skills`, `mcp`, `resume`, `results`, `chat`, `jobs`, `doclearn`): https://github.com/aadhithya/featherweight

## Before you say it works

1. The agent definitions import and build (a wrong model or effort fails at that point, with a message that says why).
2. The pipeline runs against a fake model and returns the output you expect.
3. If it must run for real, the user has agreed to the cost, and the key for the provider is set.
