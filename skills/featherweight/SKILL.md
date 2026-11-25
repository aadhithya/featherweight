---
name: featherweight
description: "Build AI agents and pipelines with the featherweight Python framework: typed Pydantic agents, a runner with retries and limits, sub-agents and background jobs, MCP tools, Agent Skills, jobs and workers with resume, streaming chat, and OpenTelemetry traces. Use when the user wants to build, extend, test or debug an app, pipeline, chat, batch job or agent system with featherweight, or when code imports `featherweight`, `featherweight.models`, `featherweight.runner` or `featherweight.model_settings`."
---

# Building with featherweight

featherweight is a small Python framework for **agents that run in pipelines and on workers**: batches, fan-out, jobs, background jobs. A chat
agent that streams its answer is a `StreamingAgent`; there is no UI and no stored conversation.
An agent is a typed class. A runner runs it with retries, limits and a trace. Your code, plain async Python, decides what
runs next. It has opinions, but they are defaults: follow them unless the user wants something else.

Install (Python 3.12 or later). Add the extras the app needs: `mcp` for MCP servers, `anthropic` for Claude, `openai` for
OpenAI, `otel` for OpenTelemetry spans (`OTelObserver`). Gemini and Gemma need no extra.

```
pip install featherweight
pip install "featherweight[all]"   # with all the extras (mcp, anthropic, openai, postgres, otel)
```

Gemini is the default model and needs `GOOGLE_API_KEY`. Claude needs `ANTHROPIC_API_KEY` and OpenAI `OPENAI_API_KEY`.

## What do you want to build?

Read the chapters of the book for the task, in order. They build one small project, an incident report assistant, and each ends with code that
runs. Paths are under `references/`; the book's home is `references/book/README.md`.

| You want to build | Read, in order |
|---|---|
| To see how the pieces fit first | `references/book/00-start-here/02-the-map.md`, `references/book/00-start-here/03-philosophy.md` |
| A typed agent, and run it | `references/book/01-your-first-agent/02-a-typed-agent.md`, `references/book/01-your-first-agent/03-running-it.md`, `references/book/01-your-first-agent/05-failures-and-check-output.md`, then `references/book/01-your-first-agent/06-choosing-a-model.md` for the provider |
| What a run recorded | `references/book/01-your-first-agent/04-what-happened.md` |
| Tools, MCP servers, skills, sub-agents for an agent | `references/book/02-abilities/01-tools.md`, `references/book/02-abilities/02-mcp.md`, `references/book/02-abilities/03-skills.md`, `references/book/02-abilities/04-sub-agents.md` |
| A pipeline over many inputs, with resume | `references/book/03-jobs-and-pipelines/01-what-a-job-is.md`, `references/book/03-jobs-and-pipelines/02-status-and-events.md`, `references/book/03-jobs-and-pipelines/03-results-and-the-result-store.md`, `references/book/03-jobs-and-pipelines/04-resume.md` |
| A streaming agent for chat, in a terminal or a browser | `references/book/04-chat/01-a-streaming-agent.md`, `references/book/04-chat/02-in-the-browser.md`, `references/book/04-chat/03-stopping-and-the-conversation.md`, `references/book/04-chat/04-attaching-a-file.md` (a file sent with a message) |
| Jobs on workers, and workers that die | `references/book/05-in-production/01-submit-and-a-worker.md`, `references/book/05-in-production/02-several-workers.md` |
| An agent that starts another and goes on | `references/book/05-in-production/03-background-jobs.md` |
| Traces, and input for an evaluation platform | `references/book/05-in-production/04-observability.md` |
| Your own storage, queue, database or observer | `references/book/06-bring-your-own-storage/01-postgres.md` to `references/book/06-bring-your-own-storage/05-an-observer.md` |
| The whole thing in one project, or a term explained | `references/book/07-reference/04-the-finished-project.md`, `references/book/07-reference/03-the-glossary.md` |

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

from featherweight import AgentBase, AgentInput, AgentOutput, Gemini


class ExtractInput(AgentInput):
    text: str


class ExtractOutput(AgentOutput):  # also carries `open_questions: list[str]`
    people: list[str] = []  # give task fields defaults, so a model that must ask a question is not forced to invent


class PeopleExtractor(AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"  # change it when the prompt or behaviour changes
    prompt: str = "List every person named in the text."
    model: Gemini = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    input_model: ClassVar[type[AgentInput]] = ExtractInput
    output_model: ClassVar[type[AgentOutput]] = ExtractOutput

    def check_output(self, agent_input, output):  # rules a schema cannot express; raise to reject and retry
        missing = [p for p in output.people if p not in agent_input.text]
        if missing:
            raise ValueError(f"names not in the text: {missing}")
```

Run it from a pipeline:

```python
import asyncio

from featherweight import AgentRunner, Status, SqliteDatabase


async def main():
    async with SqliteDatabase("featherweight.db") as db:
        job_id = await db.create_job("demo")  # runs of one pipeline share a job
        runner = AgentRunner(db)
        result = await runner.run(PeopleExtractor, ExtractInput(text="Anna Berger met Tom Okafor."), job_id=job_id)
        if result.status == Status.SUCCESS:
            print(result.output.people)
        else:
            print("failed:", result.error)  # a runtime failure is returned, not raised


asyncio.run(main())
```

`runner.run_many(Agent, inputs, job_id=...)` runs one agent over many inputs at once and returns results in input order.

## Rules that save you a round trip

- **Import what you use:** `from featherweight import AgentBase, AgentRunner`, not `import featherweight as ftw`.
- **A `job_id` is the job a run belongs to, not the run's id.** A job has many runs; a run has a `run_id`, and each of its attempts an `attempt_id`.
- **featherweight keeps metadata only** (status, tokens, timing, hashes). Inputs, outputs and results go to stores you provide
  (`ResultStore`, `Database`, `JobQueue` are interfaces you can implement), so never expect it to hold them.
- **Pass the class to the runner, not an instance.** `runner.run(PeopleExtractor, input)`. The runner builds it.
- **Check `result.status`.** It is a `Status`, a string enum: `Status.SUCCESS` or `Status.FAILED`. A failed run
  returns `FAILED` and `result.error`. It does not raise. A wrong input type does raise, and that is a bug in your code.
  `run_job(runner, job, input)` runs a job class (`@job` declares one from a function) and returns a `JobOutcome` with `job_id`, `status`, `result` (the job's output model) and `error`.
- **Two kinds of bad output.** A reply that breaks the schema (a wrong type, a value outside a `Literal`) goes back to the
  model within the same attempt. A rejection by `check_output` fails the attempt, and the runner retries it, up to 3
  attempts. Use a `Literal` or validator for rules on the output alone, `check_output` for rules about the input too.
- **Resuming a failed job:** `run_job(runner, job, input, resume_from=old_job_id)` makes a new job. Runs that
  already succeeded with the same agent and input are reused (`result.reused`, zero tokens); the rest run. The code
  between agents must be deterministic, state kept in memory is not rebuilt, and only top-level runs are reused
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
- **Capabilities are allow-lists:** `tools`, `mcp_servers`, `skills`, `subagents` and `background_jobs` are what the developer gave the agent, on purpose.
- **Check what the model returned in code.** Validators and `check_output` have the last word. The model proposes.
- **Who a run is for is an `identity`,** not something the model chooses: a small Pydantic model you define, declared with `identity_type` on the runner, the worker and the client, and passed as `identity=`. A tool reads it as `runtime.context.identity` and checks authorization itself. It carries no secrets.

## Tests and cost

- **Never call a real model in tests.** A fake is model settings: a `ModelSettings` subclass whose `build()` returns a fake
  chat model, put on a subclass of the agent under test. `references/recipes.md` has a whole test file, with a fake that
  works with `run_many`. Tests need `pytest-asyncio` with `asyncio_mode = auto`.
- Real calls cost money. Ask the user before running one, use the smallest model (`gemini-3.5-flash-lite`,
  `claude-haiku-5-5`, `gpt-6-luna`) at the lowest effort, and keep the input small.

## Look things up

The guides are the reference for one subject each (the book above is the way to learn). Read only the one for the task.

| The task | Read |
|---|---|
| Define and run an agent, results, retries, checks, tools, who is asking, configuration | `references/agents.md` |
| Patterns: a pipeline, feeding errors back, a fake model for tests, a second agent; and for a platform where users define skills or agents: a skill from a database or S3, agents built from JSON, a queue that signs messages, a tool that acts for the signed-in user (`identity`) | `references/recipes.md` |
| Pick a model or provider, effort, add GPT-5.x or Ollama | `references/models.md` |
| An agent that launches other agents | `references/launching-agents.md` |
| Use an MCP server, discovery, secrets, failures | `references/mcp.md` |
| Give an agent skills | `references/skills.md` |
| A streaming agent for chat its answer: `StreamingAgent`, `runner.stream()`, AG-UI and Vercel adapters, stopping, keeping the conversation | `references/streaming.md` |
| Attach images, PDFs, audio or video to a chat message: what is accepted, the limits, and a dated snapshot of what Anthropic, Google, OpenAI and Ollama accept | `references/streaming.md` (Attachments), `references/attachments-providers.md` |
| What the run log holds, a result store of your own, choosing a database, migrations, tenants | `references/persistence.md` |
| Declare a job (`@job`), run it with `run_job`, follow its events, resume a failed one (`resumable=True`, `resume_from`) | `references/jobs.md` |
| Submit a job to a queue and run it in a worker: `JobClient`, `JobWorker`, `JobQueue`, cancelling, events and SSE | `references/submitted-jobs.md` |
| An agent that starts another agent and goes on: `background_jobs`, `start_background`, the result message, events, a chat in the browser | `references/background-jobs.md` |
| See what jobs and agents do: observers, `OTelObserver` (OpenTelemetry spans for jobs, attempts, model calls and tool calls), content, your own spans, evaluation | `references/observability.md` |
| Deploy it: what to use where (Postgres, your own queue and result store). A suggestion only: follow the user's own stack | `references/production-stack.md` |
| See a whole pipeline of several agents | `references/example-pipeline.md` |
| Upgrade a project that uses an older featherweight: what broke, what to search for, what to write instead, and a rename table in order | `references/migration.md` |

The full specs, which say what each part does and why, are in `docs/specs` of the repository, and runnable examples are in
`examples/` (`launching`, `skills`, `mcp`, `resume`, `results`, `chat`, `jobs` (also `takeover`), `observability`, `doclearn`): https://github.com/aadhithya/featherweight

## Before you say it works

1. The agent definitions import and build (a wrong model or effort fails at that point, with a message that says why).
2. The pipeline runs against a fake model and returns the output you expect.
3. If it must run for real, the user has agreed to the cost, and the key for the provider is set.
