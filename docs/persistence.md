# Persistence: what is kept, and what is not

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

featherweight keeps **the state of the orchestration**, and none of **the data** that flowed through it. The run log says what
ran, in what order, with what outcome, at what cost. It never holds an input, an output, a message or the text of an error. Where
data has to be kept for a feature ([resuming a job](jobs.md#resuming-a-job)), you provide the place.

> The design is in the [spec](specs/persistence.md).

- [What the run log holds](#what-the-run-log-holds)
- [A result store of your own](#a-result-store-of-your-own)
- [Choosing a database](#choosing-a-database)
- [Migrations](#migrations)
- [Adding another database](#adding-another-database)
- [Tenants](#tenants)

## What the run log holds

| Kept | Never kept |
|---|---|
| Jobs: status, timing, whether a retry points to an earlier job | The input of an agent |
| Every attempt: agent, model, status, tokens, timing | The output of an agent, or the result of a job |
| The launch tree (which agent launched which) | The messages of a conversation |
| Hashes of what was declared, given and produced | The words of a prompt or a tool result |
| The type of an error, and a hash of its message | The text of an error, and the names of tools |

A hash says that something changed without saying what it was: `agent_hash` (the whole declaration, input and output schemas
included), `system_prompt_hash` (so that you can see *the prompt* changed), `tools_hash`, `input_hash`, `output_hash` and `identity_hash` (the hash of who the run was for: the identity itself is not kept). A failed
attempt keeps `error_type` (the class, such as `ValidationError`) and `error_hash`, because a message can quote data. You still get
the error text in the `AgentResult` you are returned, in the logs, and (for a streaming run) in the `RUN_ERROR` event. The columns
are below.

Content leaves featherweight in two ways only: in what `AgentRunner.run` returns to you, and through an **observer** that asks for it
([observability](observability.md)): never into the run log. The two exceptions are the sqlite queue and the sqlite result store, for
local work: they hold a submitted job's input until a worker acknowledges it, and its result. For a deployment those are yours
([a production stack](production-stack.md)).

### The tables

Two tables record everything. `jobs`: id, name, status, whether it is `resumable`, `resumed_from` (the job this one resumes, or
empty), for a submitted job its `job_hash` and `input_hash`, `cancel_requested`, the error's type and hash, and when it was created,
started and completed. A job whose task is cancelled is `CANCELLED`. `attempts`: one row **per attempt**:

| Column | |
|---|---|
| `seq`, `attempt_id` | The order the rows were made in, and the attempt |
| `run_id`, `attempt` | The attempts of one run share a `run_id` |
| `job_id` | The job |
| `parent_attempt_id`, `depth` | For an agent launched by another: the attempt that launched it, and its depth (1 for a top-level run) |
| `agent_name`, `agent_version` | The agent, readable |
| `agent_hash` | The hash of the whole agent: its prompt, version, limits, model settings (and the settings class), tools, sub-agents, skills and MCP servers, and the JSON schemas of its input and output models. Same hash, same agent |
| `system_prompt_hash`, `tools_hash` | The hash of the assembled system prompt, and of the tools bound to the model. They say *what* changed when `agent_hash` does (a tool's name can be client-specific, so tool names are not kept) |
| `provider`, `model_name`, `reasoning`, `settings`, `reported_model_name` | What was asked for (the provider, the model, the effort, all settings as JSON), and what the API reported |
| `status` | `INIT` (queued for a slot), `RUNNING`, `SUCCESS`, `FAILED` or `CANCELLED` (the task that awaited it was cancelled, or a [streaming](streaming.md) run was stopped) |
| `error_type`, `error_hash` | For a failed attempt: the class of the exception (`ValidationError`, `RateLimitError`) and the hash of its message. Recurring errors group; the text is in the logs and in the `AgentResult` you get back, not in the run log (a message can quote the data) |
| `input_hash`, `output_hash` | The SHA-256 of the input and of the output, as canonical JSON. A reused or repeated step is recognised by them, with no content kept |
| `identity_hash` | The SHA-256 of the run's identity (its kind, the name of its class, and its fields as canonical JSON without the excluded ones), `NULL` when it had none. A resume reuses only a step of the same identity; the identity itself is kept nowhere |
| `reused_from_attempt_id` | For a run that reused an earlier one ([resuming a job](jobs.md#resuming-a-job)): the row it took its output from |
| `thread_id` | For a [streaming](streaming.md) run: the conversation the client named |
| `input_tokens`, `output_tokens`, `latency_ms` | Of the attempt |
| `created_at`, `started_at`, `completed_at` | Created (queued for a slot), holds a slot and runs, ended. Times are `datetime`s |

```
sqlite3 featherweight.db "select agent_name, attempt, status, depth, error_type from attempts where job_id = '<job id>' order by seq"
```

A failed pipeline can be diagnosed from those rows (which step failed, and with what kind of error), and resumed from them
([resuming a job](jobs.md#resuming-a-job)). To see what went in and out of a step, use the logs, or keep the content yourself.

Log lines go to stdout through `setup_logging()` (`featherweight.log`), such as `attempt_started attempt_id=... attempt=1/3`.

**Why sqlite, and not OpenTelemetry or Langfuse?** To keep it as small and simple as possible: two tables, no
collector, no account. A real deployment will want a proper backend, and an observer sends traces to it
([observability](observability.md)).

## A result store of your own

Where results live (a folder, a bucket, a database per tenant) is your decision, so the store is an abstract class you implement:
`put(key, output)`, `get(key)` and `delete_job(job_id)`. The key is a `ResultKey` (the job and the hashes of the agent and the
input), and an output is a JSON `dict`. One that misses a method cannot be built, and **the conformance kit** checks the rest:

```python
class FileResultStore(ResultStore): ...  # three methods, see examples/results/store.py


await testing.check_result_store(lambda: FileResultStore(folder))  # raises AssertionError: which rule it breaks
```

[`examples/results`](../examples/results) is a whole store, in about a dozen lines (`make example-results`, no model or key).
`testing.MemoryResultStore` is the smallest, for tests. There is no Postgres result store: how results are kept in a
deployment is for you to decide.

## Choosing a database

`Database` is an abstract class, and choosing one is a line of code; the runner does not change:

```python
db = SqliteDatabase("featherweight.db")  # local development
db = PostgresDatabase("postgresql://...", schema="featherweight")  # deployment
runner = AgentRunner(db)
```

- **sqlite** is for local development. Its tables are made by numbered migrations when you connect, and a database from before
  migrations is refused: delete it.
- **Postgres** (`pip install 'featherweight[postgres]'`, `psycopg` 3) is the deployment choice. It uses native types
  (`timestamptz`, `jsonb`) and keeps its tables in their own schema, so they never collide with your application's. It has no
  default result store: pass `results=`. To try it, `docker compose up -d postgres` starts one (`postgresql://postgres:postgres@localhost:5432/postgres`),
  and `make test` runs the tests of `PostgresDatabase` against it.

Rows come back as Pydantic models (`JobRow`, `AttemptRow`) with timezone-aware `datetime`s, whichever database it is.

## Migrations

The tables are made by **numbered migrations**, written by hand for each dialect (`featherweight/migrations.py`): a version, a name, and the SQL
for sqlite and for Postgres.

- **They run when you connect.** `async with SqliteDatabase(...)` or `async with PostgresDatabase(...)` applies every migration that is not
  applied yet, in order, in whichever process connects. There is no separate command and no CLI, and a connection cannot skip them.
- **They are recorded.** Each applied migration is a row in the `schema_migrations` table (`version`, `name`, `applied_at`), so a later connect
  skips it. Each one runs in a transaction: a failing migration leaves nothing behind.
- **A new featherweight version with a schema change brings a new migration.** It is applied the first time something connects with that
  version. A migration that has been released is never edited, and there are no down-migrations.
- **Postgres creates its schema.** The first connect runs `CREATE SCHEMA IF NOT EXISTS` for the schema you named (`featherweight` by default),
  so the database user needs permission to create a schema and tables, at least that once.
- **An old database is refused.** One from before migrations existed raises `SetupError` ("delete it and run again"): nothing is carried over.
- **Processes can connect at the same moment.** On Postgres, creating the schema and applying the migrations happen under an advisory lock on the
  schema, so an API and several workers started together on a fresh database wait for each other: one makes the tables, and the rest find them done.

The rules and the reasons are in the [spec](specs/persistence.md#migrations).

## Adding another database

Implement `Database` (write every abstract method) and run the conformance kit against it:

```python
await testing.check_database(lambda: MyDatabase(...))  # an unopened database; the kit opens and closes it
```

Records go in as frozen Pydantic models (`AttemptRecord`, `AttemptFinish`), so statuses, hash formats and non-negative numbers are checked
once, for every database. The schema (every column, its type and constraint) is in the
[spec](specs/persistence.md#the-schema), and the migrations are in `featherweight/migrations.py`: a new database adds the SQL of
its dialect to each.

## Tenants

There is no tenant column: the run log holds nothing of a client's, so one database can be shared. To attribute runs to a tenant,
choose the job id (`create_job(name, job_id="acme-...")`) or the name. A result store that must separate tenants can route by
the job id of its keys, or be one per tenant.
