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
- [Resuming a job](#resuming-a-job)
- [Configuration](#configuration)

## Defining an agent

An agent is a static definition: a subclass of `AgentBase`, with Pydantic models for its input and output. Nothing is
decided at run time, so an agent is easy to read, test and version.

```python
from typing import ClassVar

import featherweight as ftw


class ExtractInput(ftw.AgentInput):
    text: str


class ExtractOutput(ftw.AgentOutput):
    people: list[str] = []


class PeopleExtractor(ftw.AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"
    prompt: str = "List every person named in the text."
    input_model: ClassVar[type[ftw.AgentInput]] = ExtractInput
    output_model: ClassVar[type[ftw.AgentOutput]] = ExtractOutput
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
import featherweight as ftw

async with ftw.Database("featherweight.db") as db:
    job_id = await db.create_job("demo")
    runner = ftw.AgentRunner(db)
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
| `reused` | `True` when the output came from the job this one resumes, with no model call ([resuming a job](#resuming-a-job)) |

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

Ready-made tools, in `featherweight.tools`:

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
import featherweight as ftw

outcome = await ftw.run_job(db, "my_pipeline", lambda job_id: my_pipeline(runner, job_id))
# outcome.status, outcome.result (on success), outcome.error ("ExceptionType: message", on failure)
```

To run in the background and hand the caller the id first, create the job with `db.create_job` and use
`asyncio.create_task(run_job(db, name, pipeline, job_id=job_id))`.

Two tables in sqlite record everything. `jobs`: id, name, status, error, created and completed times, and `resumed_from` (the job this one resumes, or empty). `agent_runs`: one
row **per attempt**:

| Column | |
|---|---|
| `agent_run_id`, `invocation_id`, `attempt` | Attempts of one `run()` call share an `invocation_id` |
| `job_id` | The job |
| `parent_agent_run_id`, `depth` | For an agent launched by another: the attempt that launched it, and its depth (1 for a top-level run) |
| `agent_name`, `agent_version`, `tool_names` | The agent. `tool_names` includes the tools the runner added |
| `model_name`, `reasoning`, `provider`, `settings`, `reported_model_name` | What was asked for (the model, the effort, the provider, all settings as JSON), and what the API reported |
| `system_prompt_hash` | The hash of the assembled system prompt |
| `agent_hash` | The hash of the whole agent: its prompt, version, limits, model settings (and the settings class), tools, sub-agents, skills and MCP servers, and the JSON schemas of its input and output models. Same hash, same agent. `NULL` for older rows |
| `status`, `error` | `INIT` (queued for a slot), `RUNNING`, `SUCCESS`, `FAILED` or `CANCELLED` (the task that awaited it was cancelled, or a [streaming](streaming.md) run was stopped) |
| `input`, `output` | JSON. A failed attempt keeps its error, not the model's raw reply |
| `input_hash` | The SHA-256 of the input as canonical JSON. `NULL` for older rows |
| `reused_from_agent_run_id` | For a run that reused an earlier one ([resuming a job](#resuming-a-job)): the row it took its output from |
| `input_tokens`, `output_tokens`, `latency_ms` | Of the attempt |
| `created_at`, `completed_at` | |

```
sqlite3 featherweight.db "select agent_name, attempt, status, depth, error from agent_runs where job_id = '<job id>' order by rowid"
```

A failed pipeline can be diagnosed from those rows, and resumed from them ([next section](#resuming-a-job)). An older
database gets new columns when it is opened.

Log lines go to stdout through `setup_logging()` (`featherweight.log`), such as `attempt_started agent_run_id=... attempt=1/3`.

**Why sqlite, and not OpenTelemetry or Langfuse?** To keep it as small and simple as possible: two tables, no
collector, no account. A real deployment will want a proper backend. Every attempt passes through one loop in
`AgentRunner` and every pipeline through `run_job`, so exporting traces is a change in those two places, and the fields
a span needs are already recorded.

## Resuming a job

A pipeline fails at step 2. Run it again with `resume_from`, and step 1 is not paid for twice. **The first job has to opt in
before it runs**, because Featherweight keeps no outputs unless you ask:

```python
first = await ftw.run_job(
    db, "my_pipeline", lambda job_id: my_pipeline(runner, job_id), resumable=True
)  # fails at step 2
outcome = await ftw.run_job(db, "my_pipeline", lambda job_id: my_pipeline(runner, job_id), resume_from=first.job_id)
```

- **`resumable=True`** keeps the output of each step of the job, in a **result store**. Without it nothing is kept, and
  `resume_from` that job raises `ValueError` ("it did not keep its results"), before any model call. To run everything
  again, run the pipeline without `resume_from`.
- **The result store** is where the outputs are kept: `AgentRunner(db, results=...)`. The default is a `SqliteResultStore`, a
  table in the run log's sqlite file, which is for local development. `results=None` keeps nothing, and then resuming a job
  is a `SetupError`. To keep outputs somewhere else (a database per tenant, a bucket), write a [`ResultStore`](#writing-a-result-store).
- **Resuming is up to you.** Nothing retries a failed job by itself. The attempts of one agent run are retried
  automatically (`max_attempts`), but a failed job is resumed by the person or the code that decides to, for example a
  retry button that calls `run_job(..., resume_from=...)`.

This creates a **new job** that records `resumed_from`, and is resumable itself, so that a third attempt is possible. Your
pipeline runs again from the top, with its own loops and decisions. Only the agent runs are answered from kept results. It is
replay, not pausing: Featherweight is not a workflow engine. To create the job first, use `db.create_job(name,
resume_from=old_job_id)` and pass its id with `job_id` (`run_job` refuses `job_id` together with `resume_from` or
`resumable`). [`examples/resume`](https://github.com/aadhithya/featherweight/blob/main/examples/resume) runs a pipeline that crashes halfway and resumes, with Claude Haiku
(`make example-resume`).

For each run that your pipeline starts in the new job, the runner looks in the old job for a row that

- has status `SUCCESS`,
- has the same `agent_hash` (the same agent: prompt, version, model settings, tools and so on),
- has the same input (`input_hash`), and
- is a top-level run (`depth` 1).

If there is one, the runner gets its output from the result store, and checks that it still validates against the output model
and passes `check_output`. If so, it does not call the model and takes no concurrency slot. It writes a `SUCCESS` row of its own in
the new job, with zero tokens, the kept output and `reused_from_agent_run_id`, copies the result to the new job (so deleting the old job
never affects it), and returns an `AgentResult` with
`reused=True` and zero tokens. The log says `run_reused ...`. If the output no longer validates, the log says
`run_reuse_rejected reason=...` and the agent runs as usual. If the store has no result for a step that the log says
succeeded (it was never kept, or was lost), the log says `result_missing` and the agent runs. A store that fails to keep a
result never fails the run: that step just cannot be reused later. With several matches (the same input twice in one
`run_many`), any one answers. `run_many` resumes per input: one failed section of ten reruns only that section.

Changing the prompt, bumping `version` or changing a model setting changes `agent_hash`, so that step runs again, and so
does any step whose input depends on its output.

What to know:

- **The code between agents must be deterministic**: no clock, no `random`, no unordered iteration. It is your code, so
  it is your responsibility, and Featherweight does not check it. If it takes another path, the agents get other inputs,
  nothing matches, and they run again.
- **The run store is not used for resumption.** Outputs come from the result store. A reused step does not run, so a
  `store_set` it made does not happen again. Do not rely on the store for anything a resume needs.
- **Side effects are not undone or repeated.** A tool that sent an email in the failed job is not rerun when its agent's
  result is reused. An agent that failed halfway runs whole again.
- **Rows from before `agent_hash` existed are never reused**, and neither are `FAILED`, `INIT` and `RUNNING` rows.
- **Depth 1 only.** A run launched by a model (a sub-agent) is never looked up. A parent that succeeded is reused whole,
  and its children never launch. A parent that failed runs again, and so do its children. Reusing children is not
  included.
- **A crash inside your pipeline code** runs that code again, and its agent calls are answered from the log.

## Writing a result store

A `ResultStore` is an abstract class with three methods: `put(key, output)`, `get(key)` and `delete_job(job_id)`. The key is a
`ResultKey` (the job, the agent hash and the input hash), and an output is a JSON `dict`. Subclass it; one that misses a method
cannot be built. `featherweight.testing.check_result_store` runs the contract against it, so that you know it behaves:

```python
await ftw.testing.check_result_store(lambda: MyStore(...))
```

`ftw.testing.MemoryResultStore` is the smallest example. Delete what you no longer need with `delete_job`.

## Configuration

| Variable | Default | |
|---|---|---|
| `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | | The key of the provider the agent's `model` uses. See [models.md](models.md) |
| `MAX_CONCURRENT_AGENT_RUNS` | 10 | Model attempts running at once, per runner. A queued attempt shows as `INIT` until it gets a slot |
| `MAX_AGENT_DEPTH` | 3 | Most agents in one chain of launches |
| `MAX_AGENT_LAUNCHES` | 10 | Most launches per attempt of one agent |
| `FTW_DB_PATH` | `featherweight.db` | The sqlite file |
| `FTW_LOG_LEVEL` | `INFO` | Used by `setup_logging()` |

The three limits can also be set on `AgentRunner`; an argument wins over the variable. A value below 1 is refused.
