# Spec: jobs and tracing

Status: implemented. How to use it is in [agents.md](../agents.md#jobs-and-tracing). The code is
[db.py](../../src/featherweight/db.py) (the abstract class, the records and rows), [sqlite.py](../../src/featherweight/sqlite.py),
[migrations.py](../../src/featherweight/migrations.py), [jobs.py](../../src/featherweight/jobs.py) and [log.py](../../src/featherweight/log.py). The
run log holds metadata only: see the [persistence spec](persistence.md) for why, and for [`PostgresDatabase`](../../src/featherweight/postgres.py).

> **Change in progress:** jobs can be submitted to a queue and run by a separate worker, with new job columns, `CANCELLED` for jobs and job events. See the [submitted jobs spec](submitted-jobs.md).

- [Goal](#goal)
- [Jobs](#jobs)
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

A job is one run of a pipeline. `run_job(db, name, pipeline, job_id=None)`:

1. creates the job (or uses `job_id`, which must exist) and marks it `RUNNING`;
2. awaits `pipeline(job_id)`;
3. marks it `SUCCESS`, or `FAILED` with the error, and returns a `JobOutcome` (`job_id`, `status`, `result` on success,
   `error` on failure).

- A pipeline that raises is a **failed outcome**, not an exception. The error is `ExceptionType: message`, up to 2,000
  characters.
- A cancelled run marks the job `FAILED` with `Cancelled` and re-raises the cancellation.
- `run_job(..., resume_from=old_job_id)` and `db.create_job(name, resume_from=old_job_id)` create a **new** job that
  records `resumed_from` (an unknown job raises `LookupError`; `run_job` refuses `job_id` and `resume_from` together).
  See [resuming a job](#resuming-a-job).
- To run in the background and hand the caller the id first: `db.create_job`, then
  `asyncio.create_task(run_job(db, name, pipeline, job_id=job_id))`.
- `set_job_status` keeps `error` only for `FAILED`, and sets `completed_at` only for `SUCCESS` and `FAILED`. A job that
  does not exist raises `LookupError`.

## Resuming a job

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

The orchestration, and no data. `ftw.Database` is an abstract class (see the [persistence spec](persistence.md)), and
`SqliteDatabase` (a file, `FTW_DB_PATH`, default `featherweight.db`) implements it for local development. Records go in
(`RunStart`, `RunEnd`) and rows come out (`JobRow`, `AgentRunRow`) as frozen Pydantic models, so the same constraints hold
for every implementation; timestamps are timezone-aware `datetime`s. The tables are made by numbered migrations, applied
when the database connects and recorded in `schema_migrations`; a database from before migrations is refused (`SetupError`).
There is **no input, output, message or error text in any of them**. Foreign keys are on.

**`jobs`**: `job_id`, `name`, `status`, `resumable` (the job keeps the outputs of its steps, so that it can be resumed),
`resumed_from` (the job this one resumes; `NULL` otherwise), `error_type`, `error_hash`, `created_at`, `completed_at`.

**`agent_runs`**, one row **per attempt**:

| Column | |
|---|---|
| `seq` | Monotonic; rows come back in this order |
| `agent_run_id` | The attempt |
| `invocation_id`, `attempt` | Attempts of one `run()` call share an `invocation_id` |
| `job_id` | The job, or empty. A job id that does not exist is refused by the foreign key |
| `parent_agent_run_id`, `depth` | For an agent launched by another: the attempt that launched it, and its depth (1 at the top) |
| `agent_name`, `agent_version` | The agent, readable |
| `agent_hash` | SHA-256 of the whole agent declaration, input and output schemas included (see [agents](agents.md)) |
| `system_prompt_hash`, `tools_hash` | SHA-256 of the assembled system prompt, and of the tools bound to the model (names, descriptions, argument schemas, and the runner's tools). They say what changed when `agent_hash` does. Tool names are not kept: they can be client-specific. The `attempt_started` log line lists them |
| `provider`, `model_name`, `reasoning`, `settings`, `reported_model_name` | What was asked for (the provider, the model, the effort or thinking level, all the settings as JSON), and what the API reported |
| `input_hash`, `output_hash` | SHA-256 of the input and of the output, as canonical JSON with sorted keys (a streaming run: of the `ChatInput` and of its text). `output_hash` is set when the run produced an output |
| `reused_from_agent_run_id` | For a run that reused an earlier one: that row. `NULL` otherwise |
| `thread_id` | For a [streaming](streaming.md) run, the conversation the client named. `NULL` otherwise |
| `status` | See below |
| `error_type`, `error_hash` | For a failed attempt: the class name of the exception, and the SHA-256 of its message. The text can quote data, so it is in the logs and the result, never the log |
| `input_tokens`, `output_tokens`, `latency_ms` | Of the attempt (non-negative) |
| `created_at`, `started_at`, `completed_at` | Created (waiting for a slot), holds a slot, ended. `completed_at - started_at` is the run; `started_at - created_at` the wait |

A reused or cancelled-while-queued run has no `started_at`. The whole launch tree of a job is one query on
`parent_agent_run_id` and `depth`. Indexes on the job, the invocation, the parent, and `(job_id, agent_hash, input_hash)`
(the lookup for reuse) keep the queries cheap.

## Statuses

| Status | Jobs | Attempts |
|---|---|---|
| `INIT` | Created | Created, waiting for a concurrency slot |
| `RUNNING` | The pipeline runs | Holds a slot and runs |
| `SUCCESS` | The pipeline returned | The output validated |
| `FAILED` | The pipeline raised, or was cancelled | The attempt failed |
| `CANCELLED` | Not used yet: a cancelled job is `FAILED` with "Cancelled" (the jobs epic changes it) | The attempt was cancelled: the task that awaited it was cancelled, a launched agent whose parent was, or a [streaming](streaming.md) run that was stopped |

`finish_run` accepts only a terminal status (`SUCCESS`, `FAILED` or `CANCELLED`).

## Logging

`setup_logging()` sends `featherweight.*` logs to stdout as plain text (`time level logger key=value ...`). The level is the
argument, else `FTW_LOG_LEVEL`, else INFO. Calling it twice adds one handler. The libraries only use
`logging.getLogger(__name__)`; entry points call `setup_logging`.

The events: `db_connected`, `db_migrated`, `job_created`, `job_status`, `job_started`, `job_finished`, `job_failed`,
`job_cancelled`, `run_created`, `run_running`, `run_finished`, `run_reused`, `run_reuse_rejected`, and the runner's lines ([runner](runner.md#logging)).
Failures log at warning or error.

## Decisions

- **sqlite, not OpenTelemetry or Langfuse.** To keep it as small as possible: two tables, no collector, no account. A real
  deployment will want a proper backend. Every attempt passes through one loop in the runner, and every pipeline through
  `run_job`, so exporting traces is a change in those two places, and the fields a span needs are already recorded.
- **One row per attempt**, so a retry is visible, and a failed attempt keeps its error.
- **The prompt is recorded as a hash**, not as text: enough to tell two runs apart, and it keeps secrets and size out of
  the table.
- **Inputs and outputs are in plaintext.** The log is for diagnosis.
- **A failed job is a value** (`JobOutcome`), the same rule as for a failed run.

## Not supported

- OpenTelemetry or another backend.
- Rows for tool calls. Skill reads and MCP calls are log lines only.
- Redaction of inputs and outputs, and retention or cleanup.
- Several processes writing one database.
- Resuming from a crash inside the pipeline's code, mid-step. The code runs again; agent runs are answered from the log.
- Reusing the runs of sub-agents, and a persistent or per-agent store.

## Tests

[test_db.py](../../tests/test_db.py) (tables, statuses, ordering, the foreign key, reopening, migration), [test_jobs.py](../../tests/test_jobs.py)
(`run_job`, failures, cancellation), [test_resume.py](../../tests/test_resume.py) (resuming a job), [test_log.py](../../tests/test_log.py), and the log rows in
[test_runner.py](../../tests/test_runner.py) and [test_subagents.py](../../tests/test_subagents.py).
