# Spec: jobs and tracing

Status: implemented. How to use it is in [agents.md](../agents.md#jobs-and-tracing). The code is
[db.py](../../src/featherweight/db.py) (the abstract class, the records and rows), [sqlite.py](../../src/featherweight/sqlite.py),
[migrations.py](../../src/featherweight/migrations.py), [jobs.py](../../src/featherweight/jobs.py) and [log.py](../../src/featherweight/log.py). The
run log holds metadata only: see the [persistence spec](persistence.md) for why, and for [`PostgresDatabase`](../../src/featherweight/postgres.py).

Jobs can also be submitted to a queue and run by a separate worker: see the [submitted jobs spec](submitted-jobs.md).

- [Goal](#goal)
- [Jobs](#jobs)
- [Running a job class](#running-a-job-class)
- [The run log](#the-run-log)
- [Statuses](#statuses)
- [Logging](#logging)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)

## Goal

Every attempt of every agent is on record, tied to the job it belongs to and to the agent that launched it, so that a
failed pipeline can be diagnosed from its rows, and replayed against them, without new model calls. A pipeline run is a
**job**.

## Jobs

A job is one run of a pipeline. It is declared as a `Job` class ([submitted jobs](submitted-jobs.md#the-job)), and it runs either in your
process or on a worker. `run_job(runner, job, input, *, job_id=None, resume_from=None, resumable=False)` runs it in your process:

1. creates the job (or uses `job_id`, which must exist), with the job's name, version, hash and the hash of the input, and starts it
   (`start_job`, the same compare-and-set step as a worker's), so it is `RUNNING`;
2. runs `job.run(ctx, input)`, where the `JobContext` holds the runner and the job's id;
3. writes the output through the runner's result store, if it has one, and marks the job `SUCCESS`, or `FAILED` with the error; it
   returns a `JobOutcome` (`job_id`, `status`, `result` (the output model) on success, `error` on failure).

- `job` is a `Job` class, or a plain (non-streaming) agent class, as for `submit`; the input must be an instance of its input model
  (`TypeError` otherwise). A plain function is not a job: `@job` makes one from it.
- A job that raises is a **failed outcome**, not an exception. The error is `ExceptionType: message`, up to 2,000 characters.
- A cancelled task marks the job `CANCELLED` and re-raises the cancellation.
- A job that was cancelled before it could start is not run: the outcome has the status the job has, no result, and an error that says so.
- `run_job(..., resume_from=old_job_id)` and `db.create_job(name, resume_from=old_job_id)` create a **new** job that
  records `resumed_from` (an unknown job raises `LookupError`; `run_job` refuses `job_id` and `resume_from` together).
  See [resuming a job](#resuming-a-job).
- To run in the background and hand the caller the id first: `db.create_job`, then
  `asyncio.create_task(run_job(runner, job, input, job_id=job_id))`.
- `set_job_status` keeps `error` only for `FAILED`, and sets `completed_at` only for `SUCCESS` and `FAILED`. A job that
  does not exist raises `LookupError`.

## Running a job class

A job is one thing: a declared class, and it runs in your process or on a worker.

```python
@job
async def summarise(ctx: JobContext, input: In) -> Out: ...


outcome = await run_job(runner, summarise, In(...))  # you run it, in your process
job_id = await client.submit(summarise, In(...))  # a worker runs it
```

- **Parity.** The only difference between running a job in your process and on a worker is **who runs it, and where**. One function,
  `execute_job`, is used by `run_job` and by the [worker](submitted-jobs.md#the-worker): it runs the started job, writes the result, sets the
  final status and so writes the events. So both write the same job row, hashes and events (`events()` cannot tell them apart); write the
  result to the result store **if there is one** (`run_job` uses the runner's, the worker its own), and keep nothing if it is `None`
  (`run_job` still returns the output); resume the same way; and fail the same way. A job whose output cannot be kept fails, because
  the output is its product.
- **What stays different:** a submitted job's input is JSON, because it crosses a queue; in your process it is the model as you built it. A
  submitted job is cancelled through the database (`client.cancel`), and one in your process by cancelling its task. The worker also settles
  the queue's message, and puts a job back on the queue when it shuts down.
- **No file, no service:** `SqliteDatabase(":memory:")` ([persistence](persistence.md#the-database)) runs it all in memory.
- **Observers.** That one function enters the observers' `job` scope around the pipeline and finishes it with the status and the error
  type, so both report the same ([the runner](runner.md#observers)); a worker also gives it the message's trace context.
- **Streaming agents** cannot be jobs, as before.
- **The timeout** (`Job.timeout`, default 600 seconds; designed, not built) applies to both: it is in the shared function ([several workers](submitted-jobs.md#several-workers)).
- **Tests:** the same job run both ways gives the same row, hashes, events and result; `:memory:`; `results=None`; resume; a failing and a
  cancelled job.

**A job has a version,** as an agent has: `Job.version`, set by hand when the pipeline's behaviour changes (`@job` takes `version=`, default
`"1"`). It is stored on the job (`jobs.job_version`) and the job's trace carries it. **A pipeline's code is not hashed:** arbitrary Python
cannot be fingerprinted in a way that means anything (closures, imports, everything they call). `job_hash` covers the declaration: the name,
the version and the two models. The code release is the user's, as the OpenTelemetry resource attribute `service.version`.

## Resuming a job

A resume is the user running a job that **ended** again, as a **new** job. (A job that did not end, because its worker died, is taken over by another worker as the **same** job: [several workers](submitted-jobs.md#several-workers). Both reuse finished steps with the mechanism below.)

A pipeline that failed at step 2 is run again with `resume_from=<failed job>`, if the failed job was created `resumable=True`
(decided before it ran; otherwise nothing was kept and `create_job` raises). This is replay with memoised agent calls, not
pausing: the pipeline's own code, loops and decisions run again, and the runner answers an agent run from the old job's
kept results when it can ([runner](runner.md#resuming) has the rules). A job created with `resume_from` is resumable too. The new job's trace has every step: a reused step
is a `SUCCESS` row with zero tokens and `reused_from_agent_run_id` pointing at the original, so the totals of the new job
count only the calls it made. The old job is not changed.

The limits:

- **The code between agents must be deterministic** (no clock, no `random`, no unordered iteration). It is the user's code
  and the user's responsibility; nothing checks it. If it takes another path, the inputs change, nothing matches, and the
  agents run again.
- **The run store is not used for resumption**; the result store is. A reused step does not run, so its `store_set` does not happen again.
- **Side effects** of tools in the failed job are not undone or repeated. An agent that failed halfway runs whole.
- **Rows from before `agent_hash`** existed are never reused. Neither are `FAILED`, `INIT` and `RUNNING` rows.
- **Depth 1 only.** Sub-agent runs are not looked up. A parent that succeeded is reused whole; one that failed runs again
  with its children. Child reuse is not included.

## The run log

The orchestration, and no data. `Database` is an abstract class (see the [persistence spec](persistence.md#the-database)), and `SqliteDatabase`
(a file, `FTW_DB_PATH`, default `featherweight.db`) implements it for local development. Records go in (`RunStart`, `RunEnd`) and rows come out
(`JobRow`, `AgentRunRow`) as frozen Pydantic models, so the same constraints hold for every implementation; timestamps are timezone-aware
`datetime`s. The tables are made by numbered migrations ([persistence](persistence.md#migrations)); a database from before migrations is refused
(`SetupError`). There is **no input, output, message or error text in any table**, except the queue's message until it is acknowledged. The
columns of every table, their types and constraints are in [the schema](persistence.md#the-schema). What they record:

- **`jobs`:** one row per job. A job made without a job class has no `job_hash` and no `input_hash`.
- **`job_events`:** one row for every change of a job's status, written in the same transaction as the change.
- **`agent_runs`:** one row **per attempt**, so a retry is visible and a failed attempt keeps its error. `invocation_id` is shared by the
  attempts of one `run()` call. `agent_hash` covers the whole agent declaration, input and output schemas included ([agents](agents.md)).
  `system_prompt_hash` and `tools_hash` say what changed between two runs. `input_hash` and `output_hash` are of the canonical JSON (a streaming
  run: of the `ChatInput` and of its text). A failed attempt records the class of the exception and the hash of its message: the text can quote
  data, so it is in the logs and the result, never the log.

`completed_at - started_at` is the run, and `started_at - created_at` the wait for a slot. A reused run, or one cancelled while queued, has no
`started_at`. The whole launch tree of a job is one query on `parent_agent_run_id` and `depth`, and the indexes on the job, the invocation, the
parent and `(job_id, agent_hash, input_hash)` (the lookup for reuse) keep the queries cheap.

## Statuses

| Status | Jobs | Attempts |
|---|---|---|
| `INIT` | Created | Created, waiting for a concurrency slot |
| `RUNNING` | The pipeline runs | Holds a slot and runs |
| `SUCCESS` | The pipeline returned | The output validated |
| `FAILED` | The pipeline raised, or was cancelled | The attempt failed |
| `CANCELLED` | The task running the pipeline was cancelled (`run_job`), or the job was cancelled before it ran | The attempt was cancelled: the task that awaited it was cancelled, a launched agent whose parent was, or a [streaming](streaming.md) run that was stopped |

`finish_run` accepts only a terminal status (`SUCCESS`, `FAILED` or `CANCELLED`).

## Logging

`setup_logging()` sends `featherweight.*` logs to stdout as plain text (`time level logger key=value ...`). The level is the
argument, else `FTW_LOG_LEVEL`, else INFO. Calling it twice adds one handler. The libraries only use
`logging.getLogger(__name__)`; entry points call `setup_logging`.

The events: `db_connected`, `db_migrated`, `job_created`, `job_status`, `job_started`, `job_finished`, `job_failed`,
`job_cancelled`, `run_created`, `run_running`, `run_finished`, `run_reused`, `run_reuse_rejected`, and the runner's lines ([runner](runner.md#logging)).
Failures log at warning or error.

## Decisions

- **sqlite for the run log, and OpenTelemetry as an observer.** The log is small: two tables, no collector, no account. A real
  deployment sends traces to its own backend through an observer ([observers and OpenTelemetry](observability.md)), and the
  log stays the source of truth.
- **One row per attempt**, so a retry is visible, and a failed attempt keeps its error.
- **The prompt is recorded as a hash**, not as text: enough to tell two runs apart, and it keeps secrets and size out of
  the table.
- **Inputs and outputs are not in the log.** It keeps metadata only ([persistence](persistence.md)); content leaves only through an observer that asks for it.
- **A failed job is a value** (`JobOutcome`), the same rule as for a failed run.

## Not supported

- Exporting traces from the log. That is an observer ([observers and OpenTelemetry](observability.md)), not a table.
- Rows for tool calls. Skill reads and MCP calls are log lines only.
- Redaction of inputs and outputs, and retention or cleanup.
- Several processes writing one database.
- Resuming from a crash inside the pipeline's code, mid-step. The code runs again; agent runs are answered from the log.
- Reusing the runs of sub-agents, and a persistent or per-agent store.

## Tests

[test_db.py](../../tests/test_db.py) (tables, statuses, ordering, the foreign key, reopening, migration), [test_jobs.py](../../tests/test_jobs.py)
(`run_job`, failures, cancellation), [test_resume.py](../../tests/test_resume.py) (resuming a job), [test_log.py](../../tests/test_log.py), and the log rows in
[test_runner.py](../../tests/test_runner.py) and [test_subagents.py](../../tests/test_subagents.py).
