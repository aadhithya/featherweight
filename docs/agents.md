# Agents

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

How to define an agent, run it, and what to expect back. Agents that launch other agents are in
[launching-agents.md](launching-agents.md), skills in [skills.md](skills.md), MCP tools in [mcp.md](mcp.md), and jobs, resuming and result stores in [jobs.md](jobs.md).

- [Defining an agent](#defining-an-agent)
- [Running an agent](#running-an-agent)
- [The result](#the-result)
- [Failures and retries](#failures-and-retries)
- [Checking the output](#checking-the-output)
- [Tools](#tools)
- [Who is asking: the identity of a run](#who-is-asking-the-identity-of-a-run)
- [Configuration](#configuration)

## Defining an agent

An agent is a static definition: a subclass of `AgentBase`, with Pydantic models for its input and output. A running agent
is complete and validated before it runs, and its hash identifies it, so it is easy to read, test and version. You write
the class, or you build it from data (a file, a database row) when you need it: the runner sees a class either way.

```python
from typing import ClassVar

from featherweight import AgentBase, AgentInput, AgentOutput


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
from featherweight import AgentRunner, SqliteDatabase

async with SqliteDatabase("featherweight.db") as db:
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
- Several agents of one pipeline share a `job_id`. Without one, the run is logged with no job. A job is for one identity ([who is asking](#who-is-asking-the-identity-of-a-run)): give every run of a `job_id` the same one.
- A `job_id` is **the job that the run belongs to**, not the id of the run. A job is one unit of work with a status (a pipeline
  you run with `run_job`, or one you submit to a worker): it has many runs, each with a `run_id`, and each attempt of a run has an `attempt_id`.

`AgentRunner(db, ...)` takes these keyword arguments:

| Argument | Default | |
|---|---|---|
| `max_attempts` | 3 | Attempts per run |
| `retry_delay_s` | 1.0 | Wait between attempts, without holding a concurrency slot |
| `max_concurrent` | `FTW_MAX_CONCURRENT_ATTEMPTS`, else 10 | Model attempts running at once |
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
| `attempt_id` | The id of the last attempt, to look it up in the run log |
| `input_tokens`, `output_tokens` | Summed over the attempt's model calls. Output includes thinking tokens, and the input includes the tool results the model read |
| `attempts`, `latency_ms` | Attempts made, and total time across them |
| `model_name` | The model as the API reported it, which can differ from the one requested |
| `reused` | `True` when the output came from the job this one resumes, with no model call ([resuming a job](jobs.md#resuming-a-job)) |

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

`tools: list[BaseTool] = [my_tool]`. A function decorated with langchain's `@tool` works. A tool that needs the job
declares `runtime: ToolRuntime[RunContext]`; langchain fills it in and hides it from the model, so the
model never passes a job id.

Ready-made tools, in `featherweight.tools`:

| Tool | |
|---|---|
| `calculate(expression)` | Exact decimal arithmetic: `+ - * / // % **` and parentheses. No functions |
| `current_datetime()` | The current UTC date and time, ISO 8601 |
| `add_to_date(date, days, weeks, months, years)` | Date arithmetic |
| `days_between(start, end)` | Whole days between two dates |

Agents that can launch agents, and agents with skills, get their extra tools from the runner; they are not in `tools`.
The tools of MCP servers are added the same way: found with `search_tools` and `call_tool` by default, or bound directly
with `discover=False` (see [mcp.md](mcp.md)).

## Who is asking: the identity of a run

A tool often has to act for someone: this teacher's mail, this tenant's files. The model must not choose who, so the application tells the run, and the tools read it.
An identity is a Pydantic model you define. You declare its type once, and pass a value with `identity=`:

```python
from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel

from featherweight import AgentRunner, RunContext


class Teacher(BaseModel):
    user_id: str
    tenant: str


@tool
async def send_email(to: str, body: str, runtime: ToolRuntime[RunContext[Teacher]]) -> str:
    """Send an email as the signed-in teacher."""
    who = runtime.context.identity  # the model never sees this parameter, and cannot set it
    if who is None or not await may_send_email(
        who.user_id
    ):  # your check: featherweight carries the identity, you decide
        return "Not allowed."
    ...


runner = AgentRunner(db, identity_type=Teacher)
result = await runner.run(Mailer, MailerInput(...), identity=Teacher(user_id="u1", tenant="uni"))
```

- **A declared type makes `identity=` required** (a `TypeError` when it is missing or of another type), so that one code path that forgets it cannot run a tool as nobody.
  `identity_type=Teacher | None` allows runs without one. With no type declared, passing an identity is a `TypeError`.
- **One agent can serve different kinds of people:** `identity_type=Teacher | Student` accepts either, and a tool that serves both annotates `RunContext[Teacher | Student]` and checks `isinstance` before it acts. Two models in one union cannot have the same class name:
  the name is the identity's kind on the queue, and a rename of the class while a job is queued fails that job.
- `run`, `run_many`, `stream` and `run_job` take `identity=`. A job reads it as `ctx.identity`, and every agent it runs with `ctx.run` has it.
- **A launched agent has its parent's identity,** and so does an agent started in the background in the same process. The model cannot set or change it.
- Code that cannot take `runtime` (a tool shared by many attempts, such as an MCP tool) reads `CURRENT_IDENTITY` from `featherweight.context`.
- **A job is for one identity.** `run_job` and `submit` give a whole job one identity. If you pass a `job_id` to `run`, `run_many` or `stream` yourself, pass the same identity every time: a run for another identity (or for none, when
  an earlier run had one) raises `ValueError` before anything is written. To run for someone else, use another job: a resume is a new job, so a support person can resume a user's job under their own identity.
- **A field marked `exclude=True` is not sent to a worker,** for a value that exists only in your process. It needs a default (`jwt: str | None = Field(None, exclude=True)`), because the worker rebuilds the identity without it; a model without one is refused when the runner, the worker or the client is built.
- **It is carried, not used.** It is never shown to the model, never part of the agent's hash, never logged and never stored. Keep it small, keep secrets out of it, and do not change it after a run has it:
  a tool gets a credential from the identity with your own code. Checking what the identity may do is the tool's job.
- A job that is submitted to a worker carries it in the queue message, and the worker rebuilds it ([submitted jobs](submitted-jobs.md#who-the-job-is-for-the-identity)).
- **Resuming a job reuses a step only for the same identity** ([resuming a job](jobs.md#resuming-a-job)): a change in who the run is for may be a change in what it may do, so the old result is not relied on. Whether a person may resume a job at all is your check.

**What stays yours,** on purpose:

- **Authorization.** A tool that ignores the identity acts for everyone. Check what the identity may do before the tool acts, and refuse in text.
- **Staleness.** The identity is a snapshot taken when the job was submitted. A queued job may run after a role changed or a user left: for a sensitive action, re-check with your application by user id.
- **Credentials.** Get a token from the identity with your own code, at each call: [a tool that gets a token for its identity](recipes/a-tool-that-gets-a-token-for-its-identity.md).
- **Who may resume a job.** A resume reuses only steps of the same identity, but whether someone may resume a job, or read its result, is a check you make with your own records of who started it.
- **Stdio MCP servers.** If users define agents, do not let them declare one ([MCP](mcp.md#declaring-a-server)).

The [identity spec](specs/identity.md) has the rules, [the book](book/05-in-production/05-who-is-asking.md) has a chapter on it, and the [recipes](recipes/README.md) show how to build on it.

## Configuration

Every environment variable of featherweight starts with `FTW_`. A few settings can also be passed in code, while you try something out: the argument wins over the variable, and the variable over the default.
The rest are environment only: they tune a deployment, and nobody sets them for one worker. A value is read when the runner or the worker is built; one that is not a positive number raises `ValueError` that names the variable.
There is no way to switch a timeout off: set a very large one.

| Variable | Default | Argument | |
|---|---|---|---|
| `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | | | The key of the provider the agent's `model` uses. See [models.md](models.md) |
| `FTW_MAX_CONCURRENT_ATTEMPTS` | 10 | `AgentRunner(max_concurrent=)` | Model attempts running at once, per runner. A queued attempt shows as `INIT` until it gets a slot |
| `FTW_MAX_AGENT_DEPTH` | 3 | `AgentRunner(max_depth=)` | Most agents in one chain of launches. 1 turns launching off |
| `FTW_MAX_AGENT_LAUNCHES` | 10 | `AgentRunner(max_launches=)` | Most launches per attempt of one agent |
| `FTW_MAX_BACKGROUND_JOBS` | 3 | `AgentRunner(max_background_jobs=)` | Most children one attempt may have running in the background |
| `FTW_WORKER_CONCURRENCY` | 10 | `JobWorker(concurrency=)` | Jobs one worker runs at once |
| `FTW_OBSERVER_TIMEOUT` | 1 s | | How long an observer hook may take before the runner goes on |
| `FTW_WORKER_RECEIVE_TIMEOUT` | 1 s | | How long a worker waits for a message before it looks again whether it is stopping |
| `FTW_WORKER_CANCEL_POLL` | 1 s | | How often a worker asks whether a job it runs was cancelled in another process |
| `FTW_JOB_LEASE` | 60 s | | How long a worker holds a job without renewing it, before another may take it over |
| `FTW_HEARTBEAT` | a third of the job's lease | | How often a worker renews the leases of its jobs and of their messages |
| `FTW_QUEUE_LEASE` | 60 s | | How long a received message stays invisible to other receivers (`SqliteQueue`) |
| `FTW_QUEUE_MAX_DELIVERIES` | 3 | | How many times `SqliteQueue` delivers a message before it marks it `dead` |
| `FTW_DB_PATH` | `featherweight.db` | the path given to `SqliteDatabase` | The sqlite file |
| `FTW_LOG_LEVEL` | `INFO` | the level given to `setup_logging()` | Used by `setup_logging()` |

`max_attempts` and `retry_delay_s` are arguments of `AgentRunner` only. The rule, and what is not configuration, is in [the configuration spec](specs/configuration.md).
