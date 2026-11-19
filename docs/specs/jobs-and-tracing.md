# Spec: jobs and tracing

Status: implemented. How to use it is in [agents.md](../agents.md#jobs-and-tracing). The code is
[db.py](../../src/featherweight/db.py), [jobs.py](../../src/featherweight/jobs.py) and [log.py](../../src/featherweight/log.py).

> **Change in progress:** `Database` becomes an abstract class with `SqliteDatabase` and `PostgresDatabase`, the schema holds metadata only (no input, output, tool names or error text), it is made by numbered migrations, and a job opts in to being resumed (`resumable`). See the [persistence spec](persistence.md).

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

A sqlite file (`FTW_DB_PATH`, default `featherweight.db`) with two tables. `Database` is an async context manager over one
connection, with foreign keys on.

**`jobs`**: `job_id`, `name`, `status`, `created_at`, `completed_at`, `error`, `resumed_from` (the job this one resumes; `NULL` otherwise).

**`agent_runs`**, one row **per attempt**:

| Column | |
|---|---|
| `agent_run_id` | The attempt |
| `invocation_id`, `attempt` | Attempts of one `run()` call share an `invocation_id` |
| `job_id` | The job, or empty. A job id that does not exist is refused by the foreign key |
| `parent_agent_run_id`, `depth` | For an agent launched by another: the attempt that launched it, and its depth (1 at the top) |
| `agent_name`, `agent_version`, `tool_names` | The agent. `tool_names` lists everything bound to the model, the runner's and MCP tools included |
| `model_name`, `reasoning`, `provider`, `settings`, `reported_model_name` | What was asked for (the model, the effort or thinking level, the provider, and all the settings as JSON; `NULL` for rows from before the last two existed), and what the API reported |
| `system_prompt_hash` | SHA-256 of the assembled system prompt |
| `agent_hash` | SHA-256 of the whole agent declaration, input and output schemas included (see [agents](agents.md)); `NULL` for rows from before it existed |
| `input_hash` | SHA-256 of the input's `model_dump(mode='json')`, serialised with sorted keys and fixed separators; `NULL` for rows from before it existed |
| `reused_from_agent_run_id` | For a run that reused an earlier one: that row. `NULL` otherwise |
| `thread_id` | For a [streaming](streaming.md) run, the conversation the client named. `NULL` otherwise |
| `output_hash` | For a streaming run, SHA-256 of its text. `NULL` otherwise |
| `status`, `error` | See below |
| `input`, `output` | JSON. A failed attempt keeps its error, not the model's raw reply. A [streaming](streaming.md) run keeps **no content**: `{"messages": n}` and `{"chars": n}` |
| `input_tokens`, `output_tokens`, `latency_ms` | Of the attempt |
| `created_at`, `completed_at` | UTC, ISO 8601 |

Rows come back in insertion order (`rowid`), filtered by `job_id` and `invocation_id` if given. The whole launch tree of a
job is one query on `parent_agent_run_id` and `depth`.

**Older databases** get the columns added after the first version (`parent_agent_run_id`, `depth`, `provider`, `settings`, `agent_hash`, `input_hash`, `reused_from_agent_run_id`, `thread_id`, `output_hash`, and
`resumed_from` on `jobs`) when they are opened. An index on `(job_id, agent_hash, input_hash)` makes the lookup for reuse
cheap.

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
