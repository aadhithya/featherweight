# Spec: the run log and the persistence boundary

Status: designed, not built. It is tracked in [epic #50](https://github.com/aadhithya/featherweight/issues/50), and each part has
its own issue (listed [at the end](#the-work)). When a part is built, this page says so, and the guide `docs/persistence.md`
shows how to use it.

It changes [jobs and tracing](jobs-and-tracing.md) (the schema, the database, the migrations), [the runner](runner.md)
(resuming reads through a result store; observers), [agents](agents.md) (resuming a job) and [streaming agents](streaming.md)
(the placeholders in the run log go away).

- [Goal](#goal)
- [The rule](#the-rule)
- [The three seams](#the-three-seams)
- [The database](#the-database)
- [Migrations](#migrations)
- [The simplified schema](#the-simplified-schema)
- [The result store](#the-result-store)
- [Resuming a job](#resuming-a-job)
- [Observers](#observers)
- [Postgres](#postgres)
- [Tenants](#tenants)
- [What changes](#what-changes)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)
- [The work](#the-work)

## Goal

Featherweight is a harness, not a data store. It keeps **the state of the orchestration**: what ran, in what order, which agent
launched which, with what outcome, at what cost, and which hashes identify the declaration and the input. It does **not** keep
**the data that flowed through it**: inputs, outputs, messages. Where data has to be kept for a feature (resume), the user
provides the place. Where it has to leave (debugging, tracing, auditing), an observer receives it.

This keeps the framework small and neutral: a deployment decides where its data lives, who can read it, and how long it stays
(a tenant's results are not in a table shared by every tenant), and the same code runs on a laptop with sqlite and on a server
with Postgres.

## The rule

| Featherweight keeps | Featherweight never keeps |
|---|---|
| Jobs and their status, and which job resumes which | The input of an agent |
| Every attempt: agent, model, status, error, tokens, timing | The output of an agent |
| The launch tree (parent, depth) | The messages of a conversation |
| Hashes: the agent declaration, the input, the output | The words of a prompt or a tool result |
| What a run matched (reused from which row), and the thread of a chat | |

The one exception is **opt-out and the user's to place**: a [result store](#the-result-store) holds the outputs that resume
needs, in a store that implements a small protocol. It defaults to the same sqlite file so that resume works with no setup; a
deployment that must not keep outputs there plugs its own, or turns it off.

`error` is metadata but may quote a fragment of what the model wrote (a validation error does). The log truncates it, and the
docs say so; an observer has the full text.

## The three seams

| Seam | Holds | Protocol | Ships |
|---|---|---|---|
| **`Database`** | Metadata: jobs, runs, lineage, tokens, status | `Database` | `SqliteDatabase`, `PostgresDatabase` |
| **`ResultStore`** | The outputs that resume reads back | `ResultStore` | `SqliteResultStore`, `PostgresResultStore` |
| **Observers** | Nothing: they receive the content of each run | `RunObserver` | `LogObserver` |

An OpenTelemetry exporter is an observer, written later. A file, Langfuse or the user's own table are observers too.

## The database

`ftw.Database` is a **protocol**: what the runner and `run_job` need, and nothing about sqlite.

| Method | |
|---|---|
| `connect()`, `close()`, `async with` | Open and close; `connect()` runs the [migrations](#migrations) |
| `create_job(name, job_id=None, resume_from=None)`, `set_job_status(...)`, `get_job(job_id)` | Jobs |
| `create_run(...)`, `mark_run_running(...)`, `finish_run(...)`, `get_run(...)`, `list_runs(job_id=..., invocation_id=...)` | Runs |
| `find_reusable_run(job_id, agent_hash, input_hash)` | The run of a job that succeeded with this agent and this input: metadata only |

`create_run` and `finish_run` take **no input and no output**: there is nothing to write them to. `ftw.SqliteDatabase(path)` and
`ftw.PostgresDatabase(dsn)` implement it, so swapping one for the other is one line:

```python
db = ftw.SqliteDatabase("featherweight.db")  # a laptop
db = ftw.PostgresDatabase("postgresql://...")  # a deployment
runner = ftw.AgentRunner(db)  # unchanged
```

Every `ftw.Database(path)` in today's docs, examples and tests becomes `ftw.SqliteDatabase(path)`. Rows come back in the order
they were made, by a monotonic `seq` column (sqlite's `rowid` has no equal everywhere).

## Migrations

The schema is made by **numbered, dialect-aware migrations**: a list of `Migration(version, name, sqlite, postgres)`, applied in
order by `connect()`, each in a transaction, and recorded in a `schema_migrations` table (`version`, `name`, `applied_at`).
This replaces the sqlite-only patching of added columns (`PRAGMA table_info`).

- There is **one baseline migration** that creates the final schema, with no migration from the shape before it. Nothing has
  been released, so no database has to be carried over: an old database is **not read**. One that has `agent_runs` but no
  `schema_migrations` raises `SetupError` ("this database is from before migrations: delete it and run again").
- A later change is a new migration with the SQL of each dialect. A migration is never edited once released.
- The dialect field exists from the start, so that the Postgres issue adds SQL, not a mechanism.

## The simplified schema

The rows lose what is data, and what another column already says.

**`agent_runs`**, one row per attempt:

| Column | |
|---|---|
| `seq` | Monotonic, for ordering |
| `agent_run_id` | The attempt |
| `invocation_id`, `attempt` | Attempts of one call share an `invocation_id` |
| `job_id` | The job, or empty |
| `parent_agent_run_id`, `depth` | The launch tree |
| `agent_name`, `agent_version` | The agent, readable |
| `agent_hash` | SHA-256 of the whole declaration (prompt, model, tools, sub-agents, skills, servers). It replaces the prompt hash and the tool names |
| `provider`, `model_name`, `reported_model_name` | What was asked for, and what the API reported |
| `settings` | All the model settings, as JSON. It replaces the effort label |
| `input_hash`, `output_hash` | SHA-256 of the input and of the output (**every** agent now, not only streaming ones) |
| `reused_from_agent_run_id` | For a run answered from an earlier one |
| `thread_id` | For a streaming run: the conversation the client named |
| `status`, `error` | `INIT`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED`; the error text (truncated) |
| `input_tokens`, `output_tokens` | Of the attempt |
| `created_at`, `started_at`, `completed_at` | Created (waiting for a slot), holds a slot, ended. `completed_at - started_at` is the run; `started_at - created_at` is the wait |

**Gone:** `input`, `output` (data), `system_prompt_hash` (inside `agent_hash`), `tool_names` (inside `agent_hash`; an observer has
the list bound to the model), `reasoning` (inside `settings`), `latency_ms` (replaced by `started_at`). The placeholders that a
streaming run used for `input` and `output` are gone with them.

**`jobs`** keeps `job_id`, `name`, `status`, `created_at`, `completed_at`, `error` and `resumed_from`. **`results`** and
**`schema_migrations`** are new (below). The jobs epic adds to `jobs` by migration.

That is 25 columns for `agent_runs` (with `seq`) instead of 29, and none of them is content.

## The result store

The outputs that resume needs go through a small protocol:

```python
class ResultStore(Protocol):
    async def put(self, key: ResultKey, output: dict[str, Any]) -> None: ...
    async def get(self, key: ResultKey) -> dict[str, Any] | None: ...
    async def delete_job(self, job_id: str) -> None: ...


@dataclass(frozen=True)
class ResultKey:
    job_id: str
    agent_hash: str
    input_hash: str
```

**The contract:** an output is JSON (a `dict`). `get` returns what `put` stored for the key, equal as JSON, or `None` when there
is none. `put` on a key that exists replaces it. Calls can be concurrent. `delete_job` removes every result of a job and does not
fail when there are none (it is what a deployment calls to keep nothing longer than it must). **A conformance kit**
(`featherweight.testing`) runs these rules against any store, so that a user's implementation is checked, not trusted.

**When a result is stored:** the output of a top-level run (depth 1) that **succeeds inside a job**. Nothing is stored for a run
with no job (it cannot be resumed), for a launched agent, or for a streaming run. A run that is answered from an earlier job
copies the result forward under its own job's key, so that a chain of resumes (job 3 resumes job 2, which resumed job 1) works.

**The default:** `AgentRunner(db, results=...)`. With a `SqliteDatabase` the default is a `SqliteResultStore` in a `results`
table of the same file (`job_id`, `agent_hash`, `input_hash`, `output`, `created_at`); with a `PostgresDatabase` it is a
`PostgresResultStore`. `results=None` stores nothing, and then a job that resumes another is a `SetupError`. Any object that
follows the protocol can be passed: a dict, a file, Redis, a bucket, a tenant's own database.

## Resuming a job

As before: `run_job(..., resume_from=old_job_id)`, and the agent runs that the old job finished are not run again. What changes is
where the output comes from:

1. The log answers the metadata question: `find_reusable_run(old_job, agent_hash, input_hash)` is a `SUCCESS` run of the old job
   at depth 1.
2. The store answers the data question: `get(ResultKey(old_job, agent_hash, input_hash))`.
3. The output is validated against the **current** output model and `check_output`, as before. If either fails, or the store
   has nothing (`result_missing`, logged), the agent runs.
4. A reused run is written as a run of its own with no tokens, `reused_from_agent_run_id` set, and its result copied forward.

The rules of [the earlier spec](jobs-and-tracing.md) stay: determinism of the code between agents is the user's, only `SUCCESS`
rows are reused, and a streaming run is never reused.

## Observers

Content leaves through observers, so that the log can hold none.

```python
class RunObserver(Protocol):
    async def on_run_started(self, event: RunStarted) -> None: ...
    async def on_run_finished(self, event: RunFinished) -> None: ...
```

`AgentRunner(db, observers=[...])`. `RunStarted` has the ids (run, invocation, job, thread, parent), the agent name, version and
hash, the provider and model, the tools bound to the model, and **the input** (a `dict`). `RunFinished` has the same ids, the
status, the **output** (a `dict`, or `None`), the error, the tokens, the timing, and `reused`. A streaming run carries its
messages (as AG-UI JSON) and its final text. There is one pair per **attempt**, and one pair for a reused run.

- Observers are called **inline, in order**. One that raises is **logged and ignored**: it never fails, retries or slows the
  verdict of a run beyond its own time (a slow one should queue its work).
- `LogObserver` logs the content at `DEBUG`, for debugging with no setup. It is off unless added.
- With none, nothing is built or sent.
- The OpenTelemetry exporter is later work, and an observer: its span events carry the content, opt-in, as OpenTelemetry's
  generative-AI conventions do for messages.

## Postgres

`ftw.PostgresDatabase(dsn, schema="featherweight")`, using `psycopg` (version 3, async), behind the extra
`featherweight[postgres]`. It runs the same migrations with the `postgres` SQL of each (placeholders, a `BIGSERIAL` `seq`,
`ON CONFLICT`). The tables live in their **own schema** (default `featherweight`), so they never collide with an application's
tables in a shared database. `PostgresResultStore(db)` is the default store for it.

Timestamps stay ISO 8601 text and JSON stays text in the first version, so that a row means the same on both databases; native
`timestamptz` and `jsonb` are a later migration. One connection per `PostgresDatabase`, like sqlite; a pool is a later step.

**Testing:** the `Database` and `ResultStore` conformance suites run against sqlite **and** Postgres. A `postgres` service in
`compose.yaml` makes the Docker unit tests run both; without `FTW_TEST_POSTGRES_URL` the Postgres cases are skipped, so a plain
`pytest` still runs.

## Tenants

There is **no tenant column and no tenant label**. A deployment that serves tenants makes **one `Database` and one `ResultStore`
per tenant** (a database or a schema each), and one runner on top of them. Nothing global holds a tenant's data, and deleting a
tenant is dropping its database, or `delete_job` for its results.

## What changes

- **API:** `ftw.Database(path)` becomes `ftw.SqliteDatabase(path)`; `ftw.Database` is the protocol. `AgentRunner(db, results=..., observers=...)`.
  `AgentRunRow` loses `input`, `output`, `system_prompt_hash`, `tool_names`, `reasoning` and `latency_ms`, and gains `started_at`
  and `output_hash`. `create_run` and `finish_run` lose `input` and `output`.
- **The log:** "Everything is on record" now means the orchestration, and the README says what is not on record. A debugging
  session that read `agent_runs.input` uses an observer (`LogObserver` is one line to add).
- **Databases:** an old `featherweight.db` is refused, not migrated. Delete it.
- **Streaming:** the placeholders in `input` and `output` go away, and an observer receives a streaming run's messages.
- **Tests and examples** that read `row.input` or `row.output` move to the result store or to an observer; the run tree printed by
  the examples uses the metadata only.

## Decisions

- **Metadata only, for every agent.** The rule is the framework's, not a streaming special case.
- **Resume reads through a `ResultStore`,** with a sqlite default so that it works with no setup, and one the user can replace.
- **The key is `(job, agent hash, input hash)`,** and a reused run copies its result forward, so that resume chains work and a
  job's results can be deleted by job.
- **One baseline migration, no carry-over:** nothing has been released.
- **`Database` is a protocol** with `SqliteDatabase` and `PostgresDatabase`; the old class name is not kept as an alias.
- **Observers are inline and isolated:** simple, ordered, and unable to break a run.
- **`psycopg` (v3, async)** for Postgres, in its own schema; text timestamps and JSON in the first version. (Taken in this spec; to
  confirm in review: `asyncpg` would be faster but speaks another placeholder syntax.)
- **`agent_version` stays,** next to the hash, because a hash is not readable. `tool_names` goes: the declaration hash covers it.
  (Taken in this spec; to confirm.)
- **No tenant label:** one instance per tenant.

## Not supported

- **Reading an old database.** Delete it.
- **Storing data in the log** for any agent, even by option. Use a result store or an observer.
- **Results for a run with no job,** a launched agent or a streaming run.
- **A TTL, size limit or encryption in the store contract:** the implementation's business.
- **Async-pool Postgres, `jsonb`, `timestamptz`:** later.
- **The OpenTelemetry exporter:** later, as an observer.
- **Jobs across processes** (queue, workers, leases): the jobs epic, which builds on this one.

## Tests

No live model call. Fake models, and fakes for the store and the observer.

- **Conformance suites** (`featherweight.testing`): the `Database` protocol and the `ResultStore` contract, against sqlite and, when
  a URL is set, Postgres.
- **Migrations:** applied once, in order, recorded; a second `connect()` does nothing; an old database is refused.
- **Metadata only:** after runs of every kind (success, failure, retry, reused, launched, streaming, stopped) no table holds an input,
  an output or a message, and no column named `input` or `output` exists.
- **Resume through the store:** success, a rejected result, a missing result, a chain of resumes, no store (`SetupError`), a run
  with no job, a launched agent, a streaming run; `delete_job`.
- **Observers:** one pair per attempt, a reused run, a launched agent, a streaming run, a raising observer, several in order.
- **The rest of the suite** moves to the new rows.

## The work

| Issue | |
|---|---|
| [#50](https://github.com/aadhithya/featherweight/issues/50) | The epic |
| [#51](https://github.com/aadhithya/featherweight/issues/51) | The `ResultStore` protocol, a sqlite default, and resume through it |
| [#52](https://github.com/aadhithya/featherweight/issues/52) | The `Database` protocol, `SqliteDatabase`, numbered migrations, the simplified metadata-only schema |
| [#53](https://github.com/aadhithya/featherweight/issues/53) | Observers |
| [#54](https://github.com/aadhithya/featherweight/issues/54) | Postgres |
| [#55](https://github.com/aadhithya/featherweight/issues/55) | The guide, an example and the skill reference |
