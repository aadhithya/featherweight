<!-- Copied from docs/agents.md by scripts/build_skill.py. Edit the original, not this file. -->

# Agents

How to define an agent, run it, and what to expect back. Agents that launch other agents are in
[launching-agents.md](launching-agents.md), skills in [skills.md](skills.md), and MCP tools in [mcp.md](mcp.md).

- [Defining an agent](#defining-an-agent)
- [Running an agent](#running-an-agent)
- [The result](#the-result)
- [Failures and retries](#failures-and-retries)
- [Checking the output](#checking-the-output)
- [Tools](#tools)
- [Jobs and tracing](#jobs-and-tracing)
- [Configuration](#configuration)

## Defining an agent

An agent is a static definition: a subclass of `AgentBase`, with Pydantic models for its input and output. Nothing is
decided at run time, so an agent is easy to read, test and version.

```python
from typing import ClassVar

from micro.models import AgentBase, AgentInput, AgentOutput


class ExtractInput(AgentInput):
    text: str


class ExtractOutput(AgentOutput):
    people: list[str] = []


class PeopleExtractor(AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"
    prompt: str = "List every person named in the text."
    input_model: ClassVar[type[AgentInput]] = ExtractInput
    output_model: ClassVar[type[AgentOutput]] = ExtractOutput
```

Pydantic does the specification work: the input is validated before the model is called, and the model's reply is
validated against the output model (it is sent as the structured-output schema). Give the output's task fields
defaults, so that a model that wants to ask a question is not forced to invent an answer just to pass validation.

### Fields of `AgentBase`

| Field | Default | |
|---|---|---|
| `name`, `description`, `version`, `prompt` | required | Recorded in the run log with every attempt. `description` is what other agents see when they may launch this one. Change `version` when the prompt or behaviour changes |
| `model` | `Gemini()` | Which model runs the agent, and how hard it thinks: `Gemini`, `Gemma`, `Claude` or `OpenAI`, or your own class. See [models.md](models.md) |
| `tools` | none | The tools the agent can call. Only these are ever bound to the model |
| `recursion_limit` | 25 | Stops a runaway tool loop inside one run; a run that hits it fails |
| `allow_user_questions` | `False` | Adds a sentence to the prompt telling the model whether it may put questions in `open_questions` |
| `raise_conflicts` | `False` | The same, for conflicts it finds in the input |
| `subagents` | none | Agents this agent may launch. See [launching-agents.md](launching-agents.md) |
| `skills` | none | Skills this agent can read. See [skills.md](skills.md) |
| `mcp_servers` | none | MCP servers whose tools this agent may use. See [mcp.md](mcp.md) |
| `input_model`, `output_model` | `AgentInput`, `AgentOutput` | Class attributes (`ClassVar`) |

`AgentInput` forbids extra fields. `AgentOutput` always carries `open_questions: list[str]`, where a model reports
questions or conflicts instead of guessing. It is never a failure.

### The system prompt

The model's system prompt is assembled from the pieces below, in this order. Its SHA-256 is recorded on every attempt
(`system_prompt_hash`), so two runs can be compared by prompt.

1. `prompt`
2. one sentence from `allow_user_questions`, and one from `raise_conflicts`
3. if the agent has `subagents` and depth left to launch: the list of agents it may launch
4. if the agent has `skills`: the list of its skills (name and description)

`agent.build_system_prompt()` returns it.

## Running an agent

```python
from micro.db import Database
from micro.runner import AgentRunner

async with Database("micro.db") as db:
    job_id = await db.create_job("demo")
    runner = AgentRunner(db)
    result = await runner.run(PeopleExtractor, ExtractInput(text="..."), job_id=job_id)
    results = await runner.run_many(
        PeopleExtractor, [ExtractInput(text="..."), ExtractInput(text="...")], job_id=job_id
    )
```

- `run(agent_cls, agent_input, job_id=None)` runs one agent. Pass the class, not an instance.
- `run_many(agent_cls, inputs, job_id=None)` runs one agent over many inputs at the same time (fan-out) and returns the
  results in input order. It has no limit of its own: the runner's concurrency limit bounds it.
- Several agents of one pipeline share a `job_id`. Without one, the run is logged with no job and has no run store.

`AgentRunner(db, ...)` takes these keyword arguments:

| Argument | Default | |
|---|---|---|
| `max_attempts` | 3 | Attempts per run |
| `retry_delay_s` | 1.0 | Wait between attempts, without holding a concurrency slot |
| `store` | a new `RunStore` | What the store tools read and write |
| `max_concurrent` | `MAX_CONCURRENT_AGENT_RUNS`, else 10 | Model attempts running at once |
| `max_depth`, `max_launches` | 3, 10 | See [launching-agents.md](launching-agents.md) |

The runner has no argument for choosing the model: an agent's own `model` settings decide it. To run an agent on a fake in
a test, give it fake model settings ([testing your agents](models.md#testing-your-agents)).

## The result

`run` always returns an `AgentResult`:

| Field | |
|---|---|
| `status` | `SUCCESS` or `FAILED` |
| `output` | The validated output model, on success |
| `error` | The last error, on failure |
| `agent_run_id` | The id of the last attempt, to look it up in the run log |
| `input_tokens`, `output_tokens` | Summed over the attempt's model calls. Output includes thinking tokens, and the input includes the tool results the model read |
| `attempts`, `latency_ms` | Attempts made, and total time across them |
| `model_name` | The model as the API reported it, which can differ from the one requested |

## Failures and retries

- **A runtime failure is a result, not an exception.** The pipeline decides what it means. Wrong input types and
  unknown job ids are programming errors, and they do raise.
- **Retries.** Every failed attempt (an API error, an invalid output, a rejected `check_output`) is retried, up to
  `max_attempts`. A retry **resamples with the same input**: the error is recorded in the log but is not sent back to
  the model. If a retry should know what went wrong, the pipeline does that (the example's Form Filler gets the
  `problems` and its `previous` attempt as input).
- **No retry** for errors that cannot succeed: a model that cannot be built (no API key, an unknown model) and any 4xx
  response other than 408, 409 and 429. These fail at once.
- **A model that returns no structured output** is a failed attempt.

## Checking the output

Beyond the schema, two places hold the rules the output must satisfy:

```python
class PeopleExtractor(AgentBase):
    ...

    def check_output(self, agent_input, output):
        missing = [p for p in output.people if p not in agent_input.text]
        if missing:
            raise ValueError(f"names not in the text: {missing}")  # a failed attempt, retried
```

- **Validators on the output model** (pydantic): rules about the output alone, such as unique ids or a value from a
  fixed set.
- **`check_output(agent_input, output)`**: rules between input and output, such as "everything the model returned is
  in the input". Raise `ValueError` to reject.

**A Gemini caveat.** A nested or nullable structured-output schema can come back silently truncated (a tree with only
its root). Prefer flat lists with ids, avoid `X | None` on nested objects, and add a `check_output` that compares the
output with the input. The example pipeline builds its trees from flat lists for this reason.

## Tools

`tools: list[BaseTool] = [my_tool]`. A function decorated with langchain's `@tool` works. A tool that needs the job or
the run store declares `runtime: ToolRuntime[RunContext]`; langchain fills it in and hides it from the model, so the
model never passes a job id.

Ready-made tools, in `micro.tools`:

| Tool | |
|---|---|
| `store_get(key)` | Read a value saved for this job, by this agent or another |
| `store_set(key, value)` | Save a text value for this job (JSON text for structured data) |
| `calculate(expression)` | Exact decimal arithmetic: `+ - * / // % **` and parentheses. No functions |
| `current_datetime()` | The current UTC date and time, ISO 8601 |
| `add_to_date(date, days, weeks, months, years)` | Date arithmetic |
| `days_between(start, end)` | Whole days between two dates |

The store is `RunStore`: state per job, in memory (`fakeredis`). Any `redis.asyncio` client can be passed to
`RunStore(client)` instead, which is the whole change to share it between processes. Pipelines use it directly too:
`await store.set(job_id, "key", value)` and `await store.get(job_id, "key")`.

Agents that can launch agents, and agents with skills, get their extra tools from the runner; they are not in `tools`.
The tools of MCP servers are added the same way: found with `search_tools` and `call_tool` by default, or bound directly
with `discover=False` (see [mcp.md](mcp.md)).

## Jobs and tracing

A **job** is one run of a pipeline. `run_job` creates it, marks it `RUNNING`, awaits your function and marks it
`SUCCESS`, or `FAILED` with the error. A pipeline that raises comes back as a failed `JobOutcome`, not an exception.

```python
from micro.jobs import run_job

outcome = await run_job(db, "my_pipeline", lambda job_id: my_pipeline(runner, job_id))
# outcome.status, outcome.result (on success), outcome.error ("ExceptionType: message", on failure)
```

To run in the background and hand the caller the id first, create the job with `db.create_job` and use
`asyncio.create_task(run_job(db, name, pipeline, job_id=job_id))`.

Two tables in sqlite record everything. `jobs`: id, name, status, error, created and completed times. `agent_runs`: one
row **per attempt**:

| Column | |
|---|---|
| `agent_run_id`, `invocation_id`, `attempt` | Attempts of one `run()` call share an `invocation_id` |
| `job_id` | The job |
| `parent_agent_run_id`, `depth` | For an agent launched by another: the attempt that launched it, and its depth (1 for a top-level run) |
| `agent_name`, `agent_version`, `tool_names` | The agent. `tool_names` includes the tools the runner added |
| `model_name`, `reasoning`, `provider`, `settings`, `reported_model_name` | What was asked for (the model, the effort, the provider, all settings as JSON), and what the API reported |
| `system_prompt_hash` | The hash of the assembled system prompt |
| `agent_hash` | The hash of the whole agent: its prompt, version, limits, model settings (and the settings class), tools, sub-agents, skills and MCP servers. Same hash, same agent. `NULL` for older rows |
| `status`, `error` | `INIT` (queued for a slot), `RUNNING`, `SUCCESS` or `FAILED` |
| `input`, `output` | JSON. A failed attempt keeps its error, not the model's raw reply |
| `input_tokens`, `output_tokens`, `latency_ms` | Of the attempt |
| `created_at`, `completed_at` | |

```
sqlite3 micro.db "select agent_name, attempt, status, depth, error from agent_runs where job_id = '<job id>' order by rowid"
```

A failed pipeline can be diagnosed from those rows, and replayed against them, without new model calls. An older
database gets new columns when it is opened.

Log lines go to stdout through `setup_logging()` (`micro.log`), such as `attempt_started agent_run_id=... attempt=1/3`.

**Why sqlite, and not OpenTelemetry or Langfuse?** To keep it as small and simple as possible: two tables, no
collector, no account. A real deployment will want a proper backend. Every attempt passes through one loop in
`AgentRunner` and every pipeline through `run_job`, so exporting traces is a change in those two places, and the fields
a span needs are already recorded.

## Configuration

| Variable | Default | |
|---|---|---|
| `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | | The key of the provider the agent's `model` uses. See [models.md](models.md) |
| `MAX_CONCURRENT_AGENT_RUNS` | 10 | Model attempts running at once, per runner. A queued attempt shows as `INIT` until it gets a slot |
| `MAX_AGENT_DEPTH` | 3 | Most agents in one chain of launches |
| `MAX_AGENT_LAUNCHES` | 10 | Most launches per attempt of one agent |
| `MICRO_DB_PATH` | `micro.db` | The sqlite file |
| `MICRO_LOG_LEVEL` | `INFO` | Used by `setup_logging()` |

The three limits can also be set on `AgentRunner`; an argument wins over the variable. A value below 1 is refused.
