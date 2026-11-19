# Spec: jobs and tracing

Status: implemented. How to use it is in [agents.md](../agents.md#jobs-and-tracing). The code is
[db.py](../../src/micro/db.py), [jobs.py](../../src/micro/jobs.py) and [log.py](../../src/micro/log.py).

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
- To run in the background and hand the caller the id first: `db.create_job`, then
  `asyncio.create_task(run_job(db, name, pipeline, job_id=job_id))`.
- `set_job_status` keeps `error` only for `FAILED`, and sets `completed_at` only for `SUCCESS` and `FAILED`. A job that
  does not exist raises `LookupError`.

## The run log

A sqlite file (`MICRO_DB_PATH`, default `micro.db`) with two tables. `Database` is an async context manager over one
connection, with foreign keys on.

**`jobs`**: `job_id`, `name`, `status`, `created_at`, `completed_at`, `error`.

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
| `agent_hash` | SHA-256 of the whole agent declaration (see [agents](agents.md)); `NULL` for rows from before it existed |
| `status`, `error` | See below |
| `input`, `output` | JSON. A failed attempt keeps its error, not the model's raw reply |
| `input_tokens`, `output_tokens`, `latency_ms` | Of the attempt |
| `created_at`, `completed_at` | UTC, ISO 8601 |

Rows come back in insertion order (`rowid`), filtered by `job_id` and `invocation_id` if given. The whole launch tree of a
job is one query on `parent_agent_run_id` and `depth`.

**Older databases** get the columns added after the first version (`parent_agent_run_id`, `depth`, `provider`, `settings`, `agent_hash`) when they are opened.

## Statuses

| Status | Jobs | Attempts |
|---|---|---|
| `INIT` | Created | Created, waiting for a concurrency slot |
| `RUNNING` | The pipeline runs | Holds a slot and runs |
| `SUCCESS` | The pipeline returned | The output validated |
| `FAILED` | The pipeline raised, or was cancelled | The attempt failed |

`finish_run` accepts only a terminal status.

## Logging

`setup_logging()` sends `micro.*` logs to stdout as plain text (`time level logger key=value ...`). The level is the
argument, else `MICRO_LOG_LEVEL`, else INFO. Calling it twice adds one handler. The libraries only use
`logging.getLogger(__name__)`; entry points call `setup_logging`.

The events: `db_connected`, `db_migrated`, `job_created`, `job_status`, `job_started`, `job_finished`, `job_failed`,
`job_cancelled`, `run_created`, `run_running`, `run_finished`, and the runner's lines ([runner](runner.md#logging)).
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
- Resuming a job from its rows.

## Tests

[test_db.py](../../tests/test_db.py) (tables, statuses, ordering, the foreign key, reopening, migration), [test_jobs.py](../../tests/test_jobs.py)
(`run_job`, failures, cancellation), [test_log.py](../../tests/test_log.py), and the log rows in
[test_runner.py](../../tests/test_runner.py) and [test_subagents.py](../../tests/test_subagents.py).
