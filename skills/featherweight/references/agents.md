<!-- Copied from docs/agents.md by scripts/build_skill.py. Edit the original, not this file. -->

# Agents

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

How to define an agent, run it, and what to expect back. Agents that launch other agents are in
[launching-agents.md](launching-agents.md), skills in [skills.md](skills.md), and MCP tools in [mcp.md](mcp.md).

- [Defining an agent](#defining-an-agent)
- [Running an agent](#running-an-agent)
- [The result](#the-result)
- [Failures and retries](#failures-and-retries)
- [Checking the output](#checking-the-output)
- [Tools](#tools)
- [Who is asking: the identity of a run](#who-is-asking-the-identity-of-a-run)
- [Jobs and tracing](#jobs-and-tracing)
- [Resuming a job](#resuming-a-job)
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
- Several agents of one pipeline share a `job_id`. Without one, the run is logged with no job.
- A `job_id` is **the job that the run belongs to**, not the id of the run. A job is one unit of work with a status (a pipeline
  you run with `run_job`, or one you submit to a worker): it has many agent runs, and each run has its own `agent_run_id`.

`AgentRunner(db, ...)` takes these keyword arguments:

| Argument | Default | |
|---|---|---|
| `max_attempts` | 3 | Attempts per run |
| `retry_delay_s` | 1.0 | Wait between attempts, without holding a concurrency slot |
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
- `run`, `run_many`, `stream` and `run_job` take `identity=`. A job reads it as `ctx.identity`, and every agent it runs with `ctx.run` has it.
- **A launched agent has its parent's identity,** and so does an agent started in the background in the same process. The model cannot set or change it.
- Code that cannot take `runtime` (a tool shared by many attempts, such as an MCP tool) reads `CURRENT_IDENTITY` from `featherweight.context`.
- **It is carried, not used.** It is never shown to the model, never part of the agent's hash, never logged and never stored. Keep it small, and keep secrets out of it:
  a tool gets a credential from the identity with your own code. Checking what the identity may do is the tool's job.
- A job that is submitted to a worker carries it in the queue message, and the worker rebuilds it ([submitted jobs](submitted-jobs.md#who-the-job-is-for-the-identity)).
- **Resuming a job reuses a step only for the same identity** ([resuming a job](#resuming-a-job)): a change in who the run is for may be a change in what it may do, so the old result is not relied on. Whether a person may resume a job at all is your check.

**What stays yours,** on purpose:

- **Authorization.** A tool that ignores the identity acts for everyone. Check what the identity may do before the tool acts, and refuse in text.
- **Staleness.** The identity is a snapshot taken when the job was submitted. A queued job may run after a role changed or a user left: for a sensitive action, re-check with your application by user id.
- **Credentials.** Get a token from the identity with your own code, at each call: [a tool that gets a token for its identity](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-tool-that-gets-a-token-for-its-identity.md).
- **Who may resume a job.** A resume reuses only steps of the same identity, but whether someone may resume a job, or read its result, is a check you make with your own records of who started it.
- **Stdio MCP servers.** If users define agents, do not let them declare one ([MCP](mcp.md#declaring-a-server)).

The [identity spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/identity.md) has the rules, [the book](book/05-in-production/05-who-is-asking.md) has a chapter on it, and the [recipes](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/README.md) show how to build on it.

## Jobs and tracing

A **job** is one run of a pipeline, declared as a class with typed input and output, like an agent. It runs in your process,
or on a worker ([submitted jobs](submitted-jobs.md)), and it is the same job either way. `run_job` creates it, marks it `RUNNING`,
runs it and marks it `SUCCESS`, or `FAILED` with the error. A job that raises comes back as a failed `JobOutcome`, not an exception.

```python
from featherweight import AgentRunner, JobContext, job, run_job


@job
async def my_pipeline(ctx: JobContext, input: PipelineInput) -> PipelineOutput:
    return PipelineOutput(...)  # plain async Python: ctx.run and ctx.run_many run agents in this job


outcome = await run_job(AgentRunner(db), my_pipeline, PipelineInput(...))
# outcome.status, outcome.result (the output model, on success), outcome.error ("ExceptionType: message", on failure)
```

The output is also written to the runner's result store if it has one (`results=None` keeps nothing; you still get it back). To
run in the background and hand the caller the id first, create the job with `db.create_job` and use
`asyncio.create_task(run_job(runner, my_pipeline, input, job_id=job_id))`. For a quick try, `SqliteDatabase(":memory:")` keeps
everything in memory: no file, no service.

**A job as a class.** A job is a declared class, like an agent (`@job`, below, builds one from a function):

```python
class Shout(Job):
    name = "shout"
    version = "1"
    input_model = ShoutInput  # Pydantic
    output_model = ShoutOutput

    async def run(self, ctx: JobContext, input: ShoutInput) -> ShoutOutput:
        result = await ctx.run(Shouter, ShouterInput(text=input.text))  # an agent, run in this job
        ...
```

The name, the version and both models are checked when the class is defined, and `Shout.job_hash()` changes when any of them (or
either schema) does. `ctx.run` and `ctx.run_many` run agents **in the job**, so they share its trace and its resume. A plain
agent can be a job too (`as_job(Shouter)`: its name, its models, and its `agent_hash`); a streaming agent cannot.

For a job that is just a function, `@job` builds the class from it, reading the models from the annotations:

```python
@job  # or @job(name="shout", version="2", timeout=120)
async def shout(ctx: JobContext, input: ShoutInput) -> ShoutOutput: ...
```

The name is the function's, the version `"1"` (the version of the job is the version of the pipeline: change it when its
behaviour changes), the timeout 600 seconds (a job that runs longer fails with `JobTimeout`; `timeout=None` for no limit), and the result is a `Job` class, usable wherever one is (the function is `shout.function`).

**The job queue.** What connects a client to a worker is an abstract class, `JobQueue` (`enqueue`, `receive`, `ack`,
`nack`, `extend`), whose message carries the job id, the job's name and its input as JSON (or references inside it). `SqliteQueue(db)`
is a table in the run log's sqlite file, for local development, and is the one we ship; for a deployment you write a queue of your
own (Redis, SQS, a database) and check it: `await testing.check_queue(lambda lease: MyQueue(...))`. `testing.MemoryQueue` is
the smallest example, for tests. Delivery is at-least-once, and a received message is leased: one whose receiver dies comes back after its lease
(60 s by default) with a higher `delivery_count`, and a receiver that is alive keeps it with `extend`.

**Submitting a job.** A client submits a job from any process, and follows it. It never runs the job: it writes the job row and
puts a message on the queue, and a separate worker runs it.

```python
client = JobClient(db, queue, results)  # results: your ResultStore, where the worker wrote the outputs
job_id = await client.submit(Shout, ShoutInput(text="hello"))  # a Job class, or a plain agent class
row = await client.status(job_id)  # the JobRow: INIT while queued
await client.wait(job_id, timeout=60)  # until it is final (a convenience for scripts and tests)
output = await client.result(Shout, job_id)  # read from YOUR result store; None until there is one
await client.cancel(job_id)  # False if the job is already final
```

`submit` checks the input against the job's model (`TypeError`), and if the queue refuses the message the job is marked `FAILED`
and the error is raised. `resume_from=failed_job_id` starts a new job that resumes it: the old job must have been
`resumable=True`, or `ValueError` says nothing was kept. `cancel` marks a queued job `CANCELLED` (its message stays on the queue,
and the worker skips it when it arrives) and asks a running job to stop (`cancel_requested`, which its worker acts on). `result`
never reads our tables, which hold no results: it asks your store, under the job's hash and input hash. `client.events(job_id)`
is the stream below.

**Following a job: job events and SSE.** Every change of a job's status is an **event** (`queued`, `running`, `succeeded`,
`failed`, `cancelled`, `cancel_requested`, and `lost`: the job's worker was lost and the job went back to the queue to be taken over), and the database writes it **in the same transaction** as the change, whoever
makes it (`run_job`, a client, a worker). An event is metadata only (`seq`, `job_id`, `type`, `status`, the error's type and hash,
`at`). `job_events(db, job_id, after=0)` streams them: the stored ones first, then each as it happens, ending after a final
one. A stream that starts late replays what it missed, and one that reconnects passes the `seq` of the last event it had.

```python
async for event in job_events(db, job_id):
    print(event.type)  # queued, running, succeeded
row = await wait_for_job(
    db, job_id, timeout=60
)  # the same stream, until the job is final (a convenience for scripts and tests)
```

In the process that wrote an event, the listener hears it at commit, with no polling. From another process, the table is read by
**one reader task per `Database` object**, with a short, growing delay, for every open stream together: a hundred open streams
cost one query, not a hundred. Polling is in that one place.

**SSE** is the one way events are streamed to a client. `sse.encode` turns the stream into server-sent events
(`id` is the `seq`, so `Last-Event-ID` resumes; a comment goes out every 15 seconds to keep the connection open) and
`sse.HEADERS` has the headers. featherweight runs no server: your API sends the response.

```python
@app.get("/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    after = int(request.headers.get("last-event-id", 0))
    return StreamingResponse(sse.encode(job_events(db, job_id, after)), headers=sse.HEADERS)
```

**Running jobs: the worker.** `JobWorker(db, queue, runner, jobs=[...], results=...)` is the process that runs submitted
jobs. It holds no state of its own: it is the database, the queue and your result store.

```python
worker = JobWorker(db, queue, runner, jobs=[Summarise, Shouter], results=my_store, concurrency=10)
await worker.run()  # until `await worker.stop()` is called
```

- **A message** names a job. The worker skips (and acknowledges) one whose job is already `CANCELLED`, fails one it has no job
  for (`UnknownJob`) or whose input does not fit the job's model (`InvalidJobInput`), and otherwise marks the job `RUNNING` and
  runs it as a task, up to `concurrency` at a time. Jobs are given as `Job` classes, or as plain agent classes.
- **Success:** the output is written through your `ResultStore` under `ResultKey(job_id, job_hash, input_hash)`, the job is
  `SUCCESS`, the message is acknowledged. If the store cannot keep it, the job is `FAILED`: the output is its product.
  `results=None` keeps nothing; the default with a `SqliteDatabase` is the sqlite store.
- **Failure:** `FAILED` with the error's type and hash (the text is in the log), and the message is acknowledged. **A failed job is
  never retried by the worker**: you resume it. A failure is an outcome, not a delivery failure, so nothing is dead-lettered.
- **Cancelling:** `worker.cancel(job_id)` cancels a job at once, in the same process. A client in another process sets
  `cancel_requested` (`request_job_cancel`), and the worker checks, every `cancel_poll` seconds and in one query for all its own
  running jobs, and cancels them. A cancelled job is `CANCELLED`.
- **Shutdown:** `await worker.stop(grace)` takes no new messages and waits up to `grace` seconds; jobs still running are cancelled
  and **returned to the queue** (back to `INIT`, the message `nack`ed with `requeue=True`), and run again from the start. If the
  task that runs the worker is cancelled, the same happens at once.
- A worker that dies without stopping leaves its messages claimed: recovering from that is for the next release.

**What is on record, and what is not.** The run log is the state of the orchestration: what ran, in what order, which agent
launched which, with what outcome, at what cost. It holds **no inputs, outputs, messages or error text**: where a name or a
text could carry a client's data, there is a hash instead. The data stays with your application.

The database is a choice of one line, and the runner does not change:

```python
db = SqliteDatabase("featherweight.db")  # local development
db = PostgresDatabase("postgresql://user:pass@host/db")  # a deployment: pip install 'featherweight[postgres]'
runner = AgentRunner(db)
```

`Database` is the abstract class that a database implements (the typed rows and records, and a conformance kit,
`testing.check_database`, check an implementation); `SqliteDatabase` and `PostgresDatabase` are the two we ship. The tables
are made by numbered migrations when you connect. A database from before migrations is refused: delete it and run again.

**Postgres** (`psycopg`, version 3) keeps its tables in a schema of their own, `featherweight` unless you pass
`schema="..."` (a lowercase identifier), so they never collide with your application's tables, and uses native types
(`timestamptz`, `jsonb`, `boolean`, an identity column). It has no default result store: where a deployment keeps the outputs
that [resuming a job](#resuming-a-job) reads back is for you to decide (a database per tenant, a table per tenant, a bucket), so
pass `results=` to the runner, or leave resume off. To try it locally, `docker compose up -d postgres` starts one, and the tests
of `PostgresDatabase` run against it when `FTW_TEST_POSTGRES_URL` is set (`make test` does).

Two tables record everything. `jobs`: id, name, status, whether it is `resumable`, `resumed_from` (the job this one resumes, or
empty), for a submitted job its `job_hash` and `input_hash`, `cancel_requested`, the error's type and hash, and when it was created,
started and completed. A job whose task is cancelled is `CANCELLED`. `agent_runs`: one row **per attempt**:

| Column | |
|---|---|
| `seq`, `agent_run_id` | The order the rows were made in, and the attempt |
| `invocation_id`, `attempt` | Attempts of one `run()` call share an `invocation_id` |
| `job_id` | The job |
| `parent_agent_run_id`, `depth` | For an agent launched by another: the attempt that launched it, and its depth (1 for a top-level run) |
| `agent_name`, `agent_version` | The agent, readable |
| `agent_hash` | The hash of the whole agent: its prompt, version, limits, model settings (and the settings class), tools, sub-agents, skills and MCP servers, and the JSON schemas of its input and output models. Same hash, same agent |
| `system_prompt_hash`, `tools_hash` | The hash of the assembled system prompt, and of the tools bound to the model. They say *what* changed when `agent_hash` does (a tool's name can be client-specific, so tool names are not kept) |
| `provider`, `model_name`, `reasoning`, `settings`, `reported_model_name` | What was asked for (the provider, the model, the effort, all settings as JSON), and what the API reported |
| `status` | `INIT` (queued for a slot), `RUNNING`, `SUCCESS`, `FAILED` or `CANCELLED` (the task that awaited it was cancelled, or a [streaming](streaming.md) run was stopped) |
| `error_type`, `error_hash` | For a failed attempt: the class of the exception (`ValidationError`, `RateLimitError`) and the hash of its message. Recurring errors group; the text is in the logs and in the `AgentResult` you get back, not in the run log (a message can quote the data) |
| `input_hash`, `output_hash` | The SHA-256 of the input and of the output, as canonical JSON. A reused or repeated step is recognised by them, with no content kept |
| `identity_hash` | The SHA-256 of the run's identity as canonical JSON (without its excluded fields), empty when it had none. A resume reuses only a step of the same identity; the identity itself is kept nowhere |
| `reused_from_agent_run_id` | For a run that reused an earlier one ([resuming a job](#resuming-a-job)): the row it took its output from |
| `thread_id` | For a [streaming](streaming.md) run: the conversation the client named |
| `input_tokens`, `output_tokens`, `latency_ms` | Of the attempt |
| `created_at`, `started_at`, `completed_at` | Created (queued for a slot), holds a slot and runs, ended. Times are `datetime`s |

```
sqlite3 featherweight.db "select agent_name, attempt, status, depth, error_type from agent_runs where job_id = '<job id>' order by seq"
```

A failed pipeline can be diagnosed from those rows (which step failed, and with what kind of error), and resumed from them
([next section](#resuming-a-job)). To see what went in and out of a step, use the logs, or keep the content yourself.

Log lines go to stdout through `setup_logging()` (`featherweight.log`), such as `attempt_started agent_run_id=... attempt=1/3`.

**Why sqlite, and not OpenTelemetry or Langfuse?** To keep it as small and simple as possible: two tables, no
collector, no account. A real deployment will want a proper backend, and an observer sends traces to it
([observability](observability.md)).

## Resuming a job

A pipeline fails at step 2. Run it again with `resume_from`, and step 1 is not paid for twice. **The first job has to opt in
before it runs**, because featherweight keeps no outputs unless you ask:

```python
first = await run_job(runner, my_pipeline, PipelineInput(...), resumable=True)  # fails at step 2
outcome = await run_job(runner, my_pipeline, PipelineInput(...), resume_from=first.job_id)
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
replay, not pausing: featherweight is not a workflow engine. To create the job first, use `db.create_job(name,
resume_from=old_job_id)` and pass its id with `job_id` (`run_job` refuses `job_id` together with `resume_from` or
`resumable`). [`examples/resume`](https://github.com/aadhithya/featherweight/blob/main/examples/resume) runs a pipeline that crashes halfway and resumes, with the default result store and Claude Haiku
(`make example-resume`).

For each run that your pipeline starts in the new job, the runner looks in the old job for a row that

- has status `SUCCESS`,
- has the same `agent_hash` (the same agent: prompt, version, model settings, tools and so on),
- has the same input (`input_hash`),
- has the same identity (`identity_hash`: [who the run is for](#who-is-asking-the-identity-of-a-run); a run with none matches only a run with none), and
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
  it is your responsibility, and featherweight does not check it. If it takes another path, the agents get other inputs,
  nothing matches, and they run again.
- **State outside the result store is not kept.** Outputs come from the result store. A reused step does not run, so
  anything it kept in memory (a counter, a dict) is not rebuilt. Do not rely on it for anything a resume needs.
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
await testing.check_result_store(lambda: MyStore(...))
```

`testing.MemoryResultStore` is the smallest example. Delete what you no longer need with `delete_job`.

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
