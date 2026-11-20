# Spec: the run log and the persistence boundary

Status: implemented (epic #58 is done: #59 the agent hash, #60 resumable jobs and the result store, #61 the `Database` abstract class and the metadata-only schema, #62 `PostgresDatabase`, #63 the guide and the example). It is tracked in [epic #58](https://github.com/aadhithya/featherweight/issues/58), and each part has
its own issue (listed [at the end](#the-work)). When a part is built, this page says so, and the guide `docs/persistence.md`
shows how to use it.

It changes [jobs and tracing](jobs-and-tracing.md) (the database, the schema, the migrations, resumable jobs), [the
runner](runner.md) (resuming reads through a result store), [agents](agents.md) (what `agent_hash` covers, resuming a job) and
[streaming agents](streaming.md) (the run log of a streaming run).

- [Goal](#goal)
- [The rule](#the-rule)
- [Hashes](#hashes)
- [Errors](#errors)
- [The database](#the-database)
- [Migrations](#migrations)
- [The schema](#the-schema)
- [The result store](#the-result-store)
- [Resuming a job](#resuming-a-job)
- [Postgres](#postgres)
- [Tenants](#tenants)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)
- [The work](#the-work)

## Goal

featherweight is a harness, not a data store. It keeps **the state of the orchestration**: what ran, in what order, which agent
launched which, with what outcome, at what cost, and which hashes identify the declaration and the input. It does **not** keep
**the data that flowed through it**: inputs, outputs, messages, results. Where data has to be kept for a feature (resuming a
job), the user provides the place.

This keeps the framework small and neutral. A deployment decides where its data lives, who can read it and how long it stays, and
the metadata can be shared and read freely because it holds nothing of a client's.

## The rule

| featherweight keeps | featherweight never keeps |
|---|---|
| Jobs: status, timing, whether a retry points to an earlier job | The input of an agent |
| Every attempt: agent, model, status, tokens, timing | The output of an agent, or the result of a job |
| The launch tree (parent, depth) | The messages of a conversation |
| Hashes of what was declared, given and produced | The words of a prompt or of a tool result |
| The type of an error, and a hash of its message | The text of an error |
| What a run matched (which row it reused), and the thread of a chat | The names of tools |

There is **one exception, and it is the user's to place**: a [result store](#the-result-store) holds the outputs that resuming a
job reads back, only for a job that opted in, in a store that the user implements. We ship one for sqlite, which is a local
development database.

## Hashes

A hash says that something changed without saying what it was, so it can stand where content or client information could show.

| Column | What it identifies | Why it is separate |
|---|---|---|
| `agent_hash` | The whole declaration: prompt, version, limits, model settings, tools, sub-agents, skills, MCP servers, the assembled system prompt, and the **input and output schemas** | Same hash, same agent |
| `system_prompt_hash` | The assembled system prompt | When a step reruns after a resume, this says *the prompt* changed |
| `tools_hash` | The tools bound to the model (names, descriptions, argument schemas) | Replaces `tool_names`: a tool's name can be client-specific. It says *the tools* changed |
| `input_hash` | The input, as canonical JSON | Matches the same input again |
| `output_hash` | The output, as canonical JSON (every agent, and a streaming run's text) | Compares two runs without keeping either |

**`agent_hash` and the schemas.** (Done in #59.) Before it, `agent_hash` left out `input_model` and `output_model`, which are class attributes and
not instance fields, so a changed output model does not change the hash. That is an oversight: the declaration includes them. Their
JSON schemas are added, and every hash changes (nothing has been released). `check_output` is code and cannot be hashed: `version`
is its signal, and a reused output is checked against it again.

## Errors

A failed run keeps `error_type` (the class name, such as `ValidationError` or `RateLimitError`) and `error_hash` (SHA-256 of the
message), not the text. Messages come from places we do not write: Pydantic quotes the offending value, `check_output` is the
user's code (the README's own example puts names in it), tools and providers say what they like. No formatting of ours can
promise that a message holds no data.

- Recurring errors still group (same type, same hash), and a failure can be told from another without the words.
- The full text is in the **logs** (a deployment keeps them if it wants to) and in the **`RUN_ERROR` event** of a streaming run,
  which the client sees.
- A job that fails keeps the same two columns.

## The database

`Database` is an **abstract class**: what the runner and `run_job` need, and nothing about sqlite. An implementation that does
not write every abstract method cannot be built.

| Method | |
|---|---|
| `connect()`, `close()`, `async with` | Open and close; `connect()` runs the [migrations](#migrations) |
| `create_job(...)`, `insert_job(...)`, `get_job(job_id)`, `set_job_status(...)` | Create, read and change a job. A change of status writes its [event](submitted-jobs.md#job-events) in the same transaction |
| `start_job(job_id)`, `cancel_queued_job(job_id)`, `fail_queued_job(job_id, error_type, error_hash)`, `request_job_cancel(job_id)`, `list_cancel_requested(job_ids)` | The compare-and-set steps that take or cancel a job, so that exactly one of a worker and a client wins ([cancelling](submitted-jobs.md#cancelling)) |
| `list_job_events(job_id, after, limit)`, `poll_job_events(job_ids, after)` | The events of one job, or of many in one query |
| `renew_job_leases(worker_id, jobs, lease)`, `owns_job(job_id, owner)` | A worker's heartbeat (one in-place update for all of its jobs, returning the ones it still owns), and a cheap read of whether it still owns one ([several workers](submitted-jobs.md#several-workers)). `set_job_status(..., owner=)` and the lease renewal are conditional on the owner, in the statement, and raise `JobLost` when it is not |
| `take_over_job(job_id, worker_id, lease)` | Taking a job whose lease expired: one transaction, a `lost` event and then a `running` event, `executions` + 1, a new lease; False if the lease is live. Where the database has a clock, it decides ([several workers](submitted-jobs.md#several-workers)) |
| `create_run(RunStart)`, `mark_run_running(...)`, `finish_run(RunEnd)`, `get_run(...)`, `list_runs(...)` | Runs |
| `find_reusable_run(job_id, agent_hash, input_hash, before_execution=None)` | The run of a job that succeeded with this agent and input: metadata only. With `before_execution`, only runs of an earlier execution of the job (a takeover looks in its own job) |

**Typed in, typed out.** `create_run` and `finish_run` take frozen Pydantic records (`RunStart`, `RunEnd`), and the methods return
Pydantic rows (`JobRow`, `AgentRunRow`). Statuses, hash formats and non-negative numbers are checked once, in the model, whichever
database sits behind it; a wrong type from an implementation fails when the row is built. Timestamps are `datetime` objects: each
implementation converts what its database stores.

**Two implementations, and a third is possible.** `SqliteDatabase(path)` is for local development, and
`PostgresDatabase(dsn)` is for deployment. Choosing is one line, and the runner does not change:

```python
db = SqliteDatabase("featherweight.db")  # local
db = PostgresDatabase("postgresql://...")  # deployed
runner = AgentRunner(db)
```

**In memory.** `SqliteDatabase(":memory:")` keeps everything (the jobs, the events, the run log and the default result store) in
memory: no file and no service, and it is gone when the database closes. It is the same code and the same migrations. It is for
trying a job and for tests .

Another database is an implementation of the class. To make that safe, the [schema](#the-schema) is documented (every column, its
type, nullability and constraint), the rows and records are models, and a **conformance kit** (`featherweight.testing`) runs the
contract against any implementation. Our two pass it.


## Migrations

The schema is made by **hand-rolled, numbered, dialect-aware migrations**: a list of `Migration(version, name, sqlite, postgres)`,
applied in order by `connect()`, each in a transaction, and recorded in `schema_migrations` (`version`, `name`, `applied_at`). The
sqlite-only patching of added columns is gone. A later change is a new migration with the SQL of each dialect, and a migration is
never edited once released.

**One baseline migration** creates the final schema. Nothing has been released and the database can be destroyed, so no data is
carried over: an old database is **not read**. One that has `agent_runs` but no `schema_migrations` raises `SetupError` ("this
database is from before migrations: delete it and run again").

## The schema

Types are given as *Postgres / sqlite*. A `CHECK` is in both.

**`agent_runs`**, one row per attempt:

| Column | Type | |
|---|---|---|
| `seq` | `bigint` identity / `integer` autoincrement | Monotonic, for ordering (sqlite's `rowid` has no equal everywhere) |
| `agent_run_id` | `text` primary key (sqlite: unique, `seq` is the key) | The attempt |
| `invocation_id` | `text` not null | Attempts of one call share it |
| `attempt` | `integer` not null, `>= 1` | |
| `job_id` | `text` null, foreign key `jobs` | |
| `parent_agent_run_id` | `text` null | The launch tree |
| `depth` | `integer` not null, `>= 1` | 1 at the top |
| `agent_name`, `agent_version` | `text` not null | The agent, readable |
| `agent_hash`, `system_prompt_hash`, `tools_hash` | `text` not null, 64 hex | See [hashes](#hashes) |
| `provider`, `model_name` | `text` not null | What was asked for |
| `reported_model_name` | `text` null | What the API reported |
| `reasoning` | `text` not null | The effort or thinking level, as a short label |
| `settings` | `jsonb` / `text` not null | All the model settings |
| `input_hash` | `text` not null, 64 hex | |
| `output_hash` | `text` null, 64 hex | Set when the run produced one |
| `reused_from_agent_run_id` | `text` null | For a run answered from an earlier one |
| `job_execution` | `integer` null | The execution of the job that made the run ([several workers](submitted-jobs.md#several-workers)): given by the task that runs the job, or else the job's `executions` when the run is created. `NULL` without a job |
| `thread_id` | `text` null | For a streaming run: the conversation the client named |
| `status` | `text` not null, one of `INIT`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED` | |
| `error_type`, `error_hash` | `text` null | Set for `FAILED` |
| `input_tokens`, `output_tokens` | `integer` null, `>= 0` | |
| `latency_ms` | `double precision` / `real` null | |
| `created_at`, `started_at`, `completed_at` | `timestamptz` / `text` (ISO 8601, UTC) | Created (waiting for a slot), holds a slot, ended |

Indexes: `job_id`, `invocation_id`, `parent_agent_run_id`, and `(job_id, agent_hash, input_hash)` for the lookup of a step to reuse.

**Gone from today's table:** `input`, `output` (data), `tool_names` (names can be client-specific; `tools_hash` replaces it), and
`error` (replaced by `error_type` and `error_hash`). **New:** `seq`, `tools_hash`, `started_at`, `error_type`, `error_hash`, and
`output_hash` for every agent. The placeholders a streaming run used for `input` and `output` are gone. The column count is
about the same: the change is in what the table holds. **Audited and kept:** `agent_version` (a hash is not readable),
`system_prompt_hash` (says *what* changed), `reasoning` and `settings`, `reported_model_name`, `latency_ms`.

**`jobs`**, one row per job:

| Column | Type | |
|---|---|---|
| `job_id` | `text` primary key | |
| `name` | `text` not null | |
| `status` | `text` not null, as for `agent_runs` | See [statuses](jobs-and-tracing.md#statuses) |
| `resumable` | `boolean` / `integer` not null, default false | Decided when the job is created: it keeps the results of its steps |
| `resumed_from` | `text` null, foreign key `jobs` | The job this one resumes |
| `job_hash`, `input_hash` | `text` null | A submitted job: the hash of its declaration and of its input. `NULL` for a job made without a job class |
| `cancel_requested` | `boolean` / `integer` not null, default false | A running job that was asked to stop |
| `worker_id`, `lease_expires_at` | `text` null, `timestamptz` / `text` null | The worker that runs the job (the last one, once it ended), and when its lease ends (`NULL` while queued, when final, and for a job run in your process). Renewed in place; Postgres uses its own clock |
| `executions` | `integer` not null, default 0, `>= 0` | How many times a worker started the job; 1 for the first, one more each time it is started again |
| `error_type`, `error_hash` | `text` null | Set for `FAILED` |
| `created_at`, `started_at`, `completed_at` | `timestamptz` / `text` | `started_at` is cleared when the job goes back to the queue |

**`job_events`**, one row for every change of a job's status, written in the same transaction as the change: `seq` (monotonic, across all
jobs), `job_id` (foreign key `jobs`), `type` (`queued`, `running`, `succeeded`, `failed`, `cancelled`, `cancel_requested`, `lost`), `status` (the status
after the change), `error_type`, `error_hash`, `at`. Index: `(job_id, seq)`. Metadata only; [submitted jobs](submitted-jobs.md#job-events) says how they are read and streamed.

**`job_queue`** (sqlite only: the queue we ship, see [the queue](submitted-jobs.md#the-queue)): `seq`, `job_id`, `job_name`, `input` (JSON: this is the one table that
holds a job's input, until the message is acknowledged), `trace_context` (JSON: the submitter's trace context, if an observer gave one;
the one place it is kept, and gone with the message), `state` (`ready`, `claimed` or `dead`), `enqueued_at`, `claimed_at`, `lease_expires_at` (when the claim's lease ends) and `delivery_count` (how many times it has been delivered).

**`results`** (the sqlite result store only): `job_id`, `agent_hash`, `input_hash` (together the primary key), `output` (`text`, JSON),
`created_at`. **`schema_migrations`:** `version` (primary key), `name`, `applied_at`.

## The result store

The outputs that resuming a job reads back go through an **abstract class** that the user implements:

```python
class ResultStore(ABC):
    @abstractmethod
    async def put(self, key: ResultKey, output: dict[str, Any]) -> None: ...
    @abstractmethod
    async def get(self, key: ResultKey) -> dict[str, Any] | None: ...
    @abstractmethod
    async def delete_job(self, job_id: str) -> None: ...


class ResultKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    agent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
```

`ResultKey` is a frozen Pydantic model, like every typed class here (records, rows, settings): no dataclasses. A malformed hash is
refused when the key is built, for every store; the conformance kit uses real hashes.

**The contract.** An output is JSON (a `dict`). `get` returns what `put` stored for the key, equal as JSON, or `None`. `put` on a key
that exists replaces it. Calls can be concurrent. `delete_job` removes every result of a job and does not fail when there are none.
A **conformance kit** (`featherweight.testing.check_result_store`) runs these rules against any store, so that an implementation is
checked, not trusted; it is how a tenant-scoped store, a bucket or Redis is verified.

**What we ship.** `SqliteResultStore` (a `results` table in the file of the run log) is the default with a `SqliteDatabase`, and
`MemoryResultStore` (a dict, in `featherweight.testing`) is for tests and is the smallest example of the class. There is **no
Postgres store**: how results are kept in a deployment (a database per tenant, a table per tenant, something else) is for the user to
decide. With a `PostgresDatabase` the default is no store: pass `results=`, or resume is unavailable.

**Opt-in, per job.** Results are stored only for a job created `resumable=True`, decided before the pipeline runs (`run_job(runner, job, input,
resumable=True)`, `db.create_job(name, resumable=True)`). A job that does not need to be resumed holds nothing. A job
created with `resume_from` is resumable too, so that a third attempt is possible.

**What is stored.** The output of a top-level run (depth 1) that succeeds inside a resumable job. Not a run with no job, a launched
agent (reusing launched agents is [parked](https://github.com/aadhithya/featherweight/issues/11)), a streaming run, or a failure.

## Resuming a job

Which steps of a failed job are reused, and the checks on them, are the runner's ([resuming](runner.md#resuming)); what a resumed job
means for a pipeline is the job's ([resuming a job](jobs-and-tracing.md#resuming-a-job)). What is persistence's is where the pieces
are kept:

1. **The old job must have been resumable.** If it was not, `create_job` raises, before any model call, saying that nothing was kept (a
   full rerun is a run without `resume_from`).
2. **The log answers the metadata question:** `find_reusable_run(old_job, agent_hash, input_hash)` is a `SUCCESS` run of the old job at
   depth 1.
3. **The store answers the data question:** `get(ResultKey(old_job, agent_hash, input_hash))`. A runner with no store, for a job that
   resumes another, is a `SetupError`.
4. **A reused step's result is copied forward** under the new job's key (a takeover has no new job, so nothing is copied: [several workers](submitted-jobs.md#several-workers)). Each job then holds its own results, deleting an old job never
   affects a newer one, and a chain of attempts works. The duplication is only the output; the trace is complete and the cost is counted once.

## Postgres

`PostgresDatabase(dsn, schema="featherweight")` implements `Database` with `psycopg` (version 3, async), behind the extra
`featherweight[postgres]` (a `SetupError` with the install line when it is missing; importing `featherweight` never imports it).
It runs the same migrations with the `postgres` SQL of each, with **native types** (`timestamptz`, `jsonb`, `boolean`, an identity
column), because Postgres is the deployment choice. The tables live in their own schema (a lowercase identifier, created when
it does not exist), so they never collide with an application's tables. The session is UTC and each statement commits (a
migration runs in one transaction). One connection per `PostgresDatabase`, like sqlite; a pool is a later step.

There is **no `results` table and no Postgres result store**: with a `PostgresDatabase` the runner has no default store, so pass
`results=` (or resume is unavailable).

**Testing:** the `Database` conformance kit, the migration tests and the type checks run against Postgres. A `postgres` service in
`compose.yaml` (a healthy dependency of the `featherweight` service, and on `127.0.0.1:5432`) makes the Docker unit tests run them
with `FTW_TEST_POSTGRES_URL` set; without it those tests are skipped, so a plain `pytest` still runs. Each test gets a schema of
its own and drops it.

## Tenants

There is **no tenant column and no tenant label**: the metadata holds nothing of a client's, so one database can be shared. A user
who needs to attribute runs to a tenant chooses the job id (`create_job(name, job_id="acme-...")`) or the job name. A result store
that must route by tenant can do so from the job id of its keys, or be one per tenant.

## Decisions

- **Metadata only, for every agent.** The rule is the framework's, not a streaming special case.
- **Hashes where names or content could show:** `tools_hash` instead of `tool_names`, `error_type` and `error_hash` instead of the text.
- **Pydantic for every typed class** (records, rows, keys, settings), not dataclasses: one way to define a typed object, with
  validation for free. (The three dataclasses that exist today are internal runtime objects, left as they are.)
- **Abstract classes** (`Database`, `ResultStore`), not protocols: an implementation that misses a method cannot be built. A conformance
  kit checks behaviour, which neither a protocol nor an abstract class can.
- **A result store only for sqlite, opt-in per job,** keyed by `(job, agent hash, input hash)`, with the result copied forward.
- **A retry is a new job** that points to the old one; resuming from a job that was not resumable is an error.
- **One baseline migration, no carry-over:** nothing has been released.
- **Native types on Postgres,** text on sqlite; the conversion is each implementation's job, and the rows are `datetime`s.
- **`psycopg` 3, own schema, hand-rolled migrations.**
- **Logs and the streaming `RUN_ERROR` keep the full error text:** a deployment keeps logs only if it wants to, and the client deals
  with what it is sent.
- **Job ids and thread ids are the user's:** we do not hash them.
- **No tenant label.**
- **Content leaves only through an observer,** one that asks for it ([observers and OpenTelemetry](observability.md)); it is never stored by us.

## Not supported

- **Reading an old database.** Delete it.
- **Content in the run log**, for any agent, even by option.
- **A Postgres result store.** The user writes one.
- **Results for a run with no job,** a launched agent, a streaming run or a job that is not resumable.
- **A TTL, size limit or encryption in the store contract:** the implementation's business.
- **A pool for Postgres:** later.
- **Jobs across processes** (queue, workers, leases): the jobs epic, which builds on this one.

## Tests

No live model call. Fake models, and the dict store.

- **Conformance kits:** the `Database` contract against sqlite and, when a URL is set, Postgres; the `ResultStore` contract against
  the sqlite store and the dict store.
- **Migrations:** applied once, in order, recorded; a second `connect()` does nothing; an old database is refused.
- **Metadata only:** after runs of every kind (success, failure, retry, reused, launched, streaming, stopped), no table holds an
  input, an output, a message or an error text, and no column named `input`, `output`, `tool_names` or `error` exists.
- **Typed models:** a wrong status, a malformed hash or a negative token count is refused when the record or row is built.
- **Resume:** success, a rejected `check_output`, a missing result, a failing `put`, a chain of three attempts with the first job's
  results deleted, no store, a non-resumable old job, nothing stored for the cases listed above.
- **The agent hash:** a change to either schema changes it.
- **The rest of the suite** moves to the new records and rows.

## The work

| Issue | |
|---|---|
| [#58](https://github.com/aadhithya/featherweight/issues/58) | The epic |
| [#59](https://github.com/aadhithya/featherweight/issues/59) | The agent hash includes the input and output schemas |
| [#60](https://github.com/aadhithya/featherweight/issues/60) | Resumable jobs and the `ResultStore`; resume through it |
| [#61](https://github.com/aadhithya/featherweight/issues/61) | The `Database` abstract class, typed records and rows, `SqliteDatabase`, migrations, the metadata-only schema |
| [#62](https://github.com/aadhithya/featherweight/issues/62) | `PostgresDatabase` |
| [#63](https://github.com/aadhithya/featherweight/issues/63) | The guide, an example and the skill reference |
