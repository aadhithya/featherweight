# Persistence: what is kept, and what is not

featherweight keeps **the state of the orchestration**, and none of **the data** that flowed through it. The run log says what
ran, in what order, with what outcome, at what cost. It never holds an input, an output, a message or the text of an error. Where
data has to be kept for a feature (resuming a job), you provide the place.

> The design is in the [spec](specs/persistence.md).

- [What the run log holds](#what-the-run-log-holds)
- [Resuming a job](#resuming-a-job)
- [A result store of your own](#a-result-store-of-your-own)
- [Choosing a database](#choosing-a-database)
- [Adding another database](#adding-another-database)
- [Tenants](#tenants)
- [Not here yet](#not-here-yet)

## What the run log holds

| Kept | Never kept |
|---|---|
| Jobs: status, timing, whether a retry points to an earlier job | The input of an agent |
| Every attempt: agent, model, status, tokens, timing | The output of an agent, or the result of a job |
| The launch tree (which agent launched which) | The messages of a conversation |
| Hashes of what was declared, given and produced | The words of a prompt or a tool result |
| The type of an error, and a hash of its message | The text of an error, and the names of tools |

A hash says that something changed without saying what it was: `agent_hash` (the whole declaration, input and output schemas
included), `system_prompt_hash` (so that you can see *the prompt* changed), `tools_hash`, `input_hash` and `output_hash`. A failed
attempt keeps `error_type` (the class, such as `ValidationError`) and `error_hash`, because a message can quote data. You still get
the error text in the `AgentResult` you are returned, in the logs, and (for a streaming run) in the `RUN_ERROR` event. The columns
are in [agents](agents.md#jobs-and-tracing).

## Resuming a job

A job keeps the outputs of its steps only if it **opts in, before it runs**:

```python
first = await run_job(runner, notes, NotesInput(...), resumable=True)  # a job (see agents.md): fails halfway
second = await run_job(
    runner, notes, NotesInput(...), resume_from=first.job_id
)  # the finished steps are not paid for again
```

The outputs go to a **result store**. With a `SqliteDatabase` the default is a table in the same file, which is for local
development; a deployment passes its own: `AgentRunner(db, results=MyStore())`. `results=None` keeps nothing. Resuming a job
that was not `resumable`, or with no store, raises before any model call. A step whose result is missing runs again. A reused step
copies its result to the new job, so deleting an old job never affects a newer one. [Resuming a job](agents.md#resuming-a-job)
has the rest, and says why resuming is always up to you.

[`examples/resume`](../examples/resume) uses the default store: it passes none. [`examples/results`](../examples/results) shows how to write your own.

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

Rows come back as Pydantic models (`JobRow`, `AgentRunRow`) with timezone-aware `datetime`s, whichever database it is.

## Adding another database

Implement `Database` (write every abstract method) and run the conformance kit against it:

```python
await testing.check_database(lambda: MyDatabase(...))  # an unopened database; the kit opens and closes it
```

Records go in as frozen Pydantic models (`RunStart`, `RunEnd`), so statuses, hash formats and non-negative numbers are checked
once, for every database. The schema (every column, its type and constraint) is in the
[spec](specs/persistence.md#the-schema), and the migrations are in `featherweight/migrations.py`: a new database adds the SQL of
its dialect to each.

## Tenants

There is no tenant column: the run log holds nothing of a client's, so one database can be shared. To attribute runs to a tenant,
choose the job id (`create_job(name, job_id="acme-...")`) or the name. A result store that must separate tenants can route by
the job id of its keys, or be one per tenant.

## Not here yet

**Observers**: a way for the content of a run (input, output, error text) to leave featherweight without being stored, for
debugging and tracing. An observer that asks for it receives the content ([observability](observability.md)); otherwise the content is in the logs only where the code logs it, and in what
`AgentRunner.run` returns to you.
