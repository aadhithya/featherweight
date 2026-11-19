# Spec: submitted jobs

Status: designed, not built. It is tracked in [epic #71](https://github.com/aadhithya/featherweight/issues/71), and each part has
its own issue (listed [at the end](#the-work)). When a part is built, this page says so, and the guide `docs/submitted-jobs.md`
shows how to use it.

It builds on [the persistence spec](persistence.md) (the `Database`, the result store, the rule that Featherweight keeps no
data). It changes [jobs and tracing](jobs-and-tracing.md) (the job columns, the `CANCELLED` status, the job events),
[the runner](runner.md) (a job context runs agents in the job) and [agents](agents.md) (jobs).

- [Goal](#goal)
- [The principle](#the-principle)
- [The job](#the-job)
- [The queue](#the-queue)
- [The client](#the-client)
- [The worker](#the-worker)
- [Cancelling](#cancelling)
- [Job events](#job-events)
- [What changes](#what-changes)
- [Decisions](#decisions)
- [Later](#later)
- [Not supported](#not-supported)
- [Tests](#tests)
- [The work](#the-work)

## Goal

Today a job is a convenience: `run_job` awaits a function you hold, in your process. A job should be something a client can
**submit**, from anywhere, that a **separate worker** picks up and runs, with the client able to follow it, cancel it, and read
its result. The client and the worker are independent: **the queue and the database are their only connection**, so they can live
in different processes, or on different machines. This spec is for **one worker**. Several workers (leases, a dead worker,
workers that compete for one queue) are the next epic, and background agents the one after.

## The principle

Featherweight passes what the user gives to the job's agents, and writes the result into the user's result store. It stores **no
inputs and no results** of its own ([the rule](persistence.md#the-rule)):

- **The input** travels in the queue message, as JSON. It can also be a reference (an id, a URL) that the job looks up itself.
- **The result** goes to the user's `ResultStore`, and the client reads it from there. Our tables hold the job's status, its
  timing, hashes and its events, and nothing of the content.

## The job

A job is a **declared class**, like an agent:

```python
class Summarise(ftw.Job):
    name = "summarise"
    version = "1"
    input_model = SummariseInput  # Pydantic
    output_model = SummariseOutput  # Pydantic

    async def run(self, ctx: ftw.JobContext, input: SummariseInput) -> SummariseOutput:
        result = await ctx.run(Summariser, SummaryInput(url=input.url))  # an agent of this job
        ...
```

- `ftw.Job` is an abstract class: one that misses `run` cannot be built, and `name`, `version` and the two models are checked when
  the class is defined.
- **`ftw.JobContext`** gives `runner`, `job_id`, and `run(...)` and `run_many(...)`, which are `runner.run` and `runner.run_many`
  with the job's id, so the agents of a job share it (and its trace, and its resume).
- **`job_hash`** is the SHA-256 of the name, the version, and the JSON schemas of both models: it says that the job changed.
- **A worker knows its jobs by name:** `JobWorker(..., jobs=[Summarise, ...])`. There is no global registry.
- **A plain agent can be a job:** a non-streaming agent class can be submitted directly. Its name is the job name, its input model
  the input, its output model the output, and `agent_hash` stands in for the job hash. A streaming agent cannot be a job (it
  streams to a person).
- **`@ftw.job`** is sugar: it builds a `Job` class from an async function, taking the name from the function (or `name=`), the
  version `"1"` (or `version=`), and the models from the annotations of `input` and the return type.
- **`run_job`** stays, as the in-process convenience for a function you hold. A job whose task is cancelled is `CANCELLED` now
  (it was `FAILED` with "CancelledError").

## The queue

`ftw.JobQueue` is an **abstract class** that connects a client to a worker:

```python
class JobMessage(BaseModel):            # frozen
    job_id: str
    job_name: str
    input: dict[str, Any]               # JSON, or references inside it

class Delivery(BaseModel):              # frozen
    message: JobMessage
    token: str                          # opaque: the implementation's own

class JobQueue(ABC):
    async def enqueue(self, message: JobMessage) -> None
    async def receive(self, timeout: float) -> Delivery | None
    async def ack(self, delivery: Delivery) -> None
    async def nack(self, delivery: Delivery, *, requeue: bool) -> None
```

**The contract:** delivery is at-least-once. A received message is invisible to other receivers until it is acknowledged or
nacked. `nack(requeue=True)` makes it available again; `nack(requeue=False)` drops it, and a broker may dead-letter it. Messages
come out in the order they went in where the implementation can. `receive` returns `None` after the timeout. Calls can be
concurrent. What happens to a message whose receiver dies is the implementation's business: ours leaves it claimed (a documented
limit until the next epic adds leases). `featherweight.testing.check_queue(make_queue)` runs the contract against any queue.

**What we ship:**
- **`SqliteQueue(db)`**: a `job_queue` table in the run log's sqlite file, for local development, and our only product
  implementation. A message is claimed atomically, `ack` deletes the row, `nack` requeues or deletes it. The payload is in the table
  until it is acknowledged, on the same terms as the sqlite result store: sqlite is for local development.
- **`MemoryQueue`**, in `featherweight.testing`, for tests.

There is no Redis, SQS or Postgres queue: how jobs travel in a deployment is the user's decision, and the abstract class and its
kit are how they check theirs. Dead-lettering is the user's broker's business.

## The client

`ftw.JobClient(db, queue, results)` submits and follows jobs, from any process:

```python
client = ftw.JobClient(db, queue, results)
job_id = await client.submit(Summarise, SummariseInput(url="https://..."))
async for event in client.events(job_id):
    ...  # queued, running, succeeded
row = await client.wait(job_id, timeout=60)  # the same stream, until a final status
output = await client.result(Summarise, job_id)  # read from YOUR result store
```

| Method | |
|---|---|
| `submit(job, input, *, resumable=False, resume_from=None, job_id=None)` | Validates the input against the job's model, creates the job row (name, `job_hash`, `input_hash`, `resumable`, `resumed_from`, and a `queued` event), then puts a `JobMessage` on the queue. If the enqueue fails the job is marked `FAILED` and the error is raised |
| `status(job_id)` | The `JobRow` |
| `cancel(job_id)` | See [cancelling](#cancelling). Returns whether there was anything to cancel |
| `result(job, job_id)` | The output, read from the user's `ResultStore` under `ResultKey(job_id, job_hash, input_hash)` and parsed into the job's output model, or `None` while there is none. It never reads our tables, which hold no results |
| `events(job_id, after=0)` | The [events](#job-events) of the job |
| `wait(job_id, timeout=None)` | The `JobRow` once the job is final (`SUCCESS`, `FAILED` or `CANCELLED`); `TimeoutError` otherwise. A convenience for scripts and tests: a web handler should return the job id, not wait |

**Resuming** is `submit(job, input, resume_from=failed_job_id)`: a new job points to the old one, and the worker's runner reuses the
finished steps exactly as `run_job(..., resume_from=...)` does. Nothing resumes a failed job by itself.

## The worker

`ftw.JobWorker(db, queue, runner, jobs, results, concurrency)` is the process that runs jobs. It holds no state of its own: it is the
database, the queue and the user's store.

`await worker.run()` loops:

1. **Receive** a message. If the job is already `CANCELLED`, acknowledge and skip it. If the job name is unknown, or the input does
   not validate, mark the job `FAILED` (`error_type`, `error_hash`) and acknowledge.
2. **Start:** mark the job `RUNNING` (`started_at`, a `running` event) and run it as an asyncio task, up to `concurrency` at a time.
3. **Success:** write the output through the user's `ResultStore` under `ResultKey(job_id, job_hash, input_hash)`, mark the job
   `SUCCESS`, acknowledge. **If the store fails to keep the result, the job is `FAILED`**, because the result is its product (a lost
   step result only reruns the step; a lost job result is the deliverable). `results=None` keeps nothing, and `client.result` has
   nothing to read.
4. **Failure:** mark the job `FAILED` with the error's type and hash, and acknowledge. **A failed job is never retried by the
   worker**: the user resumes it. The failure is a recorded outcome, not a delivery failure, so there is nothing to dead-letter.

**Shutdown.** `await worker.stop(grace)` takes no new messages, waits the grace period for running jobs, then cancels the rest and
`nack`s their messages with `requeue=True`: those jobs return to `INIT` (a `queued` event), the interrupted attempts are `CANCELLED`,
and a worker (this one, later, or another) runs them again from the start. Reusing the steps that finished before the interruption
is part of the next epic.

## Cancelling

- **A queued job** (`INIT`): `cancel` marks it `CANCELLED` (a `cancelled` event). Its message stays on the queue, because brokers
  cannot remove by id; the worker sees the status when the message arrives, acknowledges it and skips it.
- **A running job:** `cancel` sets `cancel_requested` (a `cancel_requested` event). Inside the worker each running job is an asyncio
  task, so cancelling it is `task.cancel()`, which is event-driven. **In the same process**, `worker.cancel(job_id)` does it at once.
  **From another process**, one background task of the worker checks, about every second, which of **its own** running jobs have the
  flag, in one query, and cancels them. The job is then `CANCELLED` and its interrupted attempts are `CANCELLED`.
- **A final job:** `cancel` returns `False` and changes nothing.

## Job events

The worker, the client and `run_job` all change a job's status, and every change is an **event**, for anyone who wants to follow
a job. Events are metadata only: no content (content going out is [not decided](persistence.md#not-decided-yet)).

```python
class JobEvent(BaseModel):  # frozen
    seq: int  # monotonic
    job_id: str
    type: Literal["queued", "running", "succeeded", "failed", "cancelled", "cancel_requested"]
    status: RunStatus
    error_type: str | None
    error_hash: str | None
    at: datetime
```

**The database writes the event in the same transaction as the status change** (the outbox pattern), not the worker after it: a crash
cannot leave a change without its event, and every change makes exactly one event whoever makes it. `insert_job` writes `queued`,
`set_job_status` writes the event of the new status, and `request_job_cancel(job_id)` sets the flag and writes `cancel_requested`.
`list_job_events(job_id, after)` reads them. These are methods of the `Database` abstract class, and its conformance kit covers them.

**`client.events(job_id, after=0)`** is an async stream: it replays the stored events after a sequence number, then goes live, and
ends after a final event. A listener that starts late misses nothing, and one that reconnects resumes from its last `seq`.
`wait` reads the status and, if the job is not final, consumes the stream, so there is no gap between checking and listening.

**How it is delivered.** In the same process the database wakes the listeners when the transaction commits: instantly, with no
polling. Across processes the events table must be read, so **one reader task per process** polls it with a growing delay (one
query for every open stream) and fans out to them: a hundred open connections cost one query. The polling is inside the library,
in one place.

**SSE is the only streaming protocol.** `ftw.sse.encode(events)` turns the stream into server-sent events and `ftw.sse.HEADERS`
has the headers:

```
id: 12
event: running
data: {"seq":12,"job_id":"…","status":"RUNNING","at":"2026-11-20T10:00:01Z"}
```

`id` is the sequence number, so a browser that reconnects sends `Last-Event-ID` and the stream resumes after it. A comment line goes
out every 15 seconds to keep the connection open, and the stream ends after a final event. Featherweight runs **no server**: the
user's API sends the response (a `StreamingResponse` in FastAPI, about a dozen lines, in the example). No WebSockets, one job per
stream, no stream of all jobs.

## What changes

- **`jobs` table** (the baseline migration is edited: nothing has been released): `job_hash`, `input_hash` (null for jobs made by
  `run_job`), `started_at`, `cancel_requested` (default false). **`job_events`** and **`job_queue`** (sqlite only) are new tables.
  `JobRow` gains the columns.
- **`CANCELLED` is a status of jobs** (it was unused for jobs).
- **`Database`:** `create_job` takes `job_hash` and `input_hash`; new `request_job_cancel` and `list_job_events`; `set_job_status`
  writes events and sets `started_at`; going back to `INIT` clears it. The conformance kit and `PostgresDatabase` follow.
- **New in the package:** `Job`, `JobContext`, `job` (the decorator), `JobQueue`, `JobMessage`, `Delivery`, `SqliteQueue`,
  `JobClient`, `JobWorker`, `JobEvent`, and `ftw.sse`; `MemoryQueue` and `check_queue` in `featherweight.testing`.
- **Not changed:** `run_job`, resuming, the result store, the agents.

## Decisions

- **Everything is passed, nothing is kept:** inputs in the message, results in the user's store, and the metadata in ours.
- **A job is a declared class,** registered with the worker by name, with a decorator as sugar. An agent can be a job.
- **`JobQueue` is an abstract class** with sqlite as our only product implementation and a conformance kit; the user brings Redis,
  SQS or anything else.
- **Client and worker are independent:** the queue and the database are their only connection.
- **A failed job is not retried by the worker;** the user resumes it. A failed job is an outcome, not a delivery failure.
- **A lost job result fails the job;** a lost step result only reruns the step.
- **Events are written by the database, in the transaction of the status change,** and streamed with SSE only.
- **Polling is centralised, not removed:** one reader per process, behind `events()`.
- **Cancel:** a queued job is marked and skipped; a running job's flag is checked by the worker about every second.
- **Shutdown:** a grace period, then cancel and requeue.

## Later

- **An event channel as an abstract class,** like `Database`: Postgres `LISTEN/NOTIFY`, Redis pub/sub or a cloud broker would remove
  the polling across processes. Pub/sub is vendor-specific and not available everywhere, so we do not write one per vendor now.
  If it is wanted, it is one abstract class behind the same `events()`, with a conformance kit.
- **Several workers, leases, heartbeats, a dead worker, redelivery that reuses finished steps:** the next epic.
- **Background agents:** the epic after, built on the job events.

## Not supported

- **A durable queue we ship** beyond sqlite, and a pub/sub channel.
- **Automatic retry of a failed job.**
- **Several workers on one queue,** and recovering from a worker that died with a message claimed.
- **Progress with content** in events, and a stream of all jobs.
- **A server, a router or WebSockets.** The example shows an SSE endpoint.
- **Pruning events:** the table grows, and pruning is the user's for now.

## Tests

No live model call. Fake models, `MemoryQueue`, the dict result store.

- **The job class:** the contract, the hash, an agent as a job, a streaming agent refused, `@ftw.job`.
- **The queue:** `check_queue` against `SqliteQueue` and `MemoryQueue`, two receivers on one queue.
- **Events:** every status change makes exactly one event, whoever makes it; a late listener replays; the same-process wake-up; a
  writer and a reader on one file in two processes; one reader serving many streams; the stream ends at a final event; the SSE
  format and `Last-Event-ID`; the `Database` conformance kit on sqlite and Postgres.
- **The client and the worker:** submit, status, wait, result; a failing enqueue; every cancel case, in the same process and from
  another; success, failure, an unknown job, an invalid input, a failing result store, a skipped cancelled job; shutdown with the
  grace period and the requeue; concurrency; resuming through `submit`; and a client in one process with a worker in another.

## The work

| Issue | |
|---|---|
| [#71](https://github.com/aadhithya/featherweight/issues/71) | The epic |
| [#72](https://github.com/aadhithya/featherweight/issues/72) | The `Job` class, `JobContext`, the job columns and `CANCELLED` |
| [#73](https://github.com/aadhithya/featherweight/issues/73) | The `JobQueue` abstract class, `SqliteQueue`, `MemoryQueue` and the kit |
| [#74](https://github.com/aadhithya/featherweight/issues/74) | Job events and SSE |
| [#75](https://github.com/aadhithya/featherweight/issues/75) | `JobClient` |
| [#76](https://github.com/aadhithya/featherweight/issues/76) | `JobWorker` |
| [#77](https://github.com/aadhithya/featherweight/issues/77) | The `@ftw.job` decorator |
| [#78](https://github.com/aadhithya/featherweight/issues/78) | The guide, an example and the skill reference |
