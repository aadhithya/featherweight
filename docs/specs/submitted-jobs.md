# Spec: submitted jobs

Status: implemented, including [several workers](#several-workers) ([epic #103](https://github.com/aadhithya/featherweight/issues/103)): the queue's lease (#104), the job lease, the heartbeat and the owner check (#105), takeover (#106), the job timeout (#107), and the guide, the example and the skill reference (#108). #72 (the `Job` class, `JobContext`, the job columns and `CANCELLED`), #73 (the queue), #74 (job events and SSE), #75 (the client), #76 (the worker, with the compare-and-set steps for taking and cancelling a job), #77 (the `@job` decorator) and #78 (the guide, the example and the skill reference) are done. It is tracked in [epic #71](https://github.com/aadhithya/featherweight/issues/71), and each part has
its own issue (listed [at the end](#the-work)). When a part is built, this page says so, and the guide `docs/submitted-jobs.md`
shows how to use it.

It builds on [the persistence spec](persistence.md) (the `Database`, the result store, the rule that featherweight keeps no
data). It changes [jobs and tracing](jobs-and-tracing.md) (the job columns, the `CANCELLED` status, the job events),
[the runner](runner.md) (a job context runs agents in the job) and [agents](agents.md) (jobs).

- [Goal](#goal)
- [The principle](#the-principle)
- [The job](#the-job)
- [The queue](#the-queue)
- [The client](#the-client)
- [The worker](#the-worker)
- [Several workers](#several-workers)
- [Cancelling](#cancelling)
- [Job events](#job-events)
- [Decisions](#decisions)
- [Later](#later)
- [Not supported](#not-supported)
- [Tests](#tests)
- [The work](#the-work)

## Goal

A job is a declared class that runs in your process (`run_job`) or on a worker, by the same code. It should be something a client can
**submit**, from anywhere, that a **separate worker** picks up and runs, with the client able to follow it, cancel it, and read
its result. The client and the worker are independent: **the queue and the database are their only connection**, so they can live
in different processes, or on different machines. It covers the queue, the client and the worker, and **several workers** on one queue
(leases, a dead worker: [several workers](#several-workers)). Background agents have [their own spec](background-agents.md).

## The principle

featherweight passes what the user gives to the job's agents, and writes the result into the user's result store. It stores **no
inputs and no results** of its own ([the rule](persistence.md#the-rule)):

- **The input** travels in the queue message, as JSON. It can also be a reference (an id, a URL) that the job looks up itself.
- **The result** goes to the user's `ResultStore`, and the client reads it from there. Our tables hold the job's status, its
  timing, hashes and its events, and nothing of the content.

## The job

A job is a **declared class**, like an agent:

```python
class Summarise(Job):
    name = "summarise"
    version = "1"
    input_model = SummariseInput  # Pydantic
    output_model = SummariseOutput  # Pydantic

    async def run(self, ctx: JobContext, input: SummariseInput) -> SummariseOutput:
        result = await ctx.run(Summariser, SummaryInput(url=input.url))  # an agent of this job
        ...
```

- `Job` is an abstract class: one that misses `run` cannot be built, and `name`, `version` and the two models are checked when
  the class is defined.
- **`JobContext`** gives `runner`, `job_id`, and `run(...)` and `run_many(...)`, which are `runner.run` and `runner.run_many`
  with the job's id, so the agents of a job share it (and its trace, and its resume).
- **`job_hash`** is the SHA-256 of the name, the version, and the JSON schemas of both models: it says that the job changed.
- **A worker knows its jobs by name:** `JobWorker(..., jobs=[Summarise, ...])`. There is no global registry.
- **A plain agent can be a job:** a non-streaming agent class can be submitted directly. Its name is the job name, its input model
  the input, its output model the output, and `agent_hash` stands in for the job hash. A streaming agent cannot be a job (it
  streams to a person).
- **`timeout`**: how long one execution of the job may run, in seconds. The default is 600 (ten minutes), and `None` means no limit. It is
  checked when the class is defined (a positive number, or `None`), it is not part of `job_hash`, and `@job(timeout=...)` sets it. An agent used
  as a job has the default. See [the timeout](#several-workers).
- **`@job`** is sugar: it builds a `Job` class from an async function, taking the name from the function (or `name=`), the
  version `"1"` (or `version=`), and the models from the annotations of `input` and the return type.
- **`run_job`** runs a `Job` class in your process, with the same code as the worker ([running a job
  class](jobs-and-tracing.md#running-a-job-class)). A job whose task is cancelled is `CANCELLED`.

## The queue

`JobQueue` is an **abstract class** that connects a client to a worker:

```python
class JobMessage(BaseModel):            # frozen
    job_id: str
    job_name: str
    input: dict[str, Any]               # JSON, or references inside it
    trace_context: dict[str, str] = {}  # the W3C trace context of the submitter, if an observer gave one (see below)

class Delivery(BaseModel):              # frozen
    message: JobMessage
    token: str                          # opaque: the implementation's own
    delivery_count: int = 1             # how many times this message has been delivered, this one included

class JobQueue(ABC):
    async def enqueue(self, message: JobMessage) -> None
    async def receive(self, timeout: float) -> Delivery | None
    async def ack(self, delivery: Delivery) -> None
    async def nack(self, delivery: Delivery, *, requeue: bool) -> None
    async def extend(self, delivery: Delivery) -> None   # renew the lease of a received message
```

**The contract:** delivery is at-least-once. A received message is invisible to other receivers until it is acknowledged or
nacked. `nack(requeue=True)` makes it available again; `nack(requeue=False)` drops it, and a broker may dead-letter it. Messages
come out in the order they went in where the implementation can. `receive` returns `None` after the timeout. Calls can be
concurrent. **A lease** (built: [#104](https://github.com/aadhithya/featherweight/issues/104)): a received message is invisible to other receivers for a limited
time, and not for ever. If it is neither acknowledged, nacked nor `extend`ed before the lease ends, it becomes available again,
with a higher `delivery_count`, so that a worker that died does not hold a message for ever. `extend` renews the lease, and a stale delivery (one whose message was delivered again since) changes nothing. These are
what real brokers already have (SQS's visibility timeout, `ChangeMessageVisibility` and receive count; a Redis stream's pending list), so an implementation
over one is thin. How many redeliveries are allowed is the queue's business. `featherweight.testing.check_queue(make_queue)` runs the contract against any queue; `make_queue(lease)` makes a fresh queue with that lease in seconds, and the kit uses a short one to see a message come back.

**What we ship:**
- **`SqliteQueue(db)`**: a `job_queue` table in the run log's sqlite file, for local development, and our only product
  implementation. A message is claimed atomically, `ack` deletes the row, `nack` requeues or deletes it. With the lease: `SqliteQueue(db, lease=60, max_deliveries=3)`; a message whose lease ends is delivered again (`ready` or `claimed` with an expired lease, in the order it went in), and one that was delivered `max_deliveries` times and whose lease ends again is marked `dead`, kept in the table for inspection, and logged (`queue_message_dead`). Failing its job (`DeadLettered`) is part of [#106](https://github.com/aadhithya/featherweight/issues/106), which is not built yet. A delivery's token names its claim (`<seq>:<delivery count>`), so that a stale `ack`, `nack` or `extend` cannot touch a message that was delivered again. A `nack(requeue=True)` is a deliberate return, and does not count against `max_deliveries`. The payload is in the table
  until it is acknowledged, on the same terms as the sqlite result store: sqlite is for local development.
- **`MemoryQueue(lease=60)`**, in `featherweight.testing`, for tests: the same lease semantics (pass a very short lease in a test), and no limit on redeliveries.

There is no Redis, SQS or Postgres queue: how jobs travel in a deployment is the user's decision, and the abstract class and its
kit are how they check theirs. Dead-lettering is the user's broker's business.

The **trace context** ([#93](https://github.com/aadhithya/featherweight/issues/93), built) is how a worker's span joins the trace of
the client that submitted the job ([observers and OpenTelemetry](observability.md#opentelemetry)). It is empty unless the client has an observer
that gives one, and it travels in the message only: it is stored in no table. Every queue carries it, and `check_queue` checks that it does.

## The client

`JobClient(db, queue, results, observers=[...])` submits and follows jobs, from any process. The observers are heard on `submit` only: each can return a
carrier (the trace context) that travels in the message ([the queue](#the-queue)), and an observer that raises changes nothing.

```python
client = JobClient(db, queue, results)
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

`JobWorker(db, queue, runner, jobs, results, concurrency)` is the process that runs jobs. It holds no state of its own: it is the
database, the queue and the user's store.

`await worker.run()` loops:

1. **Receive** a message. If the job is already `CANCELLED`, acknowledge and skip it. If the job name is unknown, or the input does
   not validate, mark the job `FAILED` (`error_type`, `error_hash`) and acknowledge.
2. **Start:** take the job (`start_job`: `RUNNING`, `started_at`, a `running` event) and run it as an asyncio task, up to `concurrency` at a time. What the
   task does is `execute_job`, the function that also runs a job for `run_job` ([running a job class](jobs-and-tracing.md#running-a-job-class)): the worker adds the queue.
3. **Success:** write the output through the user's `ResultStore` under `ResultKey(job_id, job_hash, input_hash)`, mark the job
   `SUCCESS`, acknowledge. **If the store fails to keep the result, the job is `FAILED`**, because the result is its product (a lost
   step result only reruns the step; a lost job result is the deliverable). `results=None` keeps nothing, and `client.result` has
   nothing to read.
4. **Failure:** mark the job `FAILED` with the error's type and hash, and acknowledge. **A failed job is never retried by the
   worker**: the user resumes it. The failure is a recorded outcome, not a delivery failure, so there is nothing to dead-letter.

**Shutdown.** `await worker.stop(grace)` takes no new messages, waits the grace period for running jobs, then cancels the rest and
`nack`s their messages with `requeue=True`: those jobs return to `INIT` (a `queued` event), the interrupted attempts are `CANCELLED`,
and a worker (this one, later, or another) runs them again, as a takeover does ([several workers](#several-workers)): the steps that finished
before the interruption are reused if the job is `resumable`.

## Several workers

**Built** ([epic #103](https://github.com/aadhithya/featherweight/issues/103), with one issue for each part: [#104](https://github.com/aadhithya/featherweight/issues/104) the queue, [#105](https://github.com/aadhithya/featherweight/issues/105) the job lease and the owner check, [#106](https://github.com/aadhithya/featherweight/issues/106) takeover, [#107](https://github.com/aadhithya/featherweight/issues/107) the timeout, [#108](https://github.com/aadhithya/featherweight/issues/108) the guide). Several workers on one queue already work today: a message is claimed by one receiver, `start_job` is a
compare-and-set that exactly one worker wins, a cancel is checked by the worker that owns the job, the events have one reader per
process, and a shutdown requeues. What does not work is a worker that **dies**: its message stays claimed, and its job stays `RUNNING`
for ever. This section is the recovery from that, and it is all that it is: we do not build a broker, a scheduler or a registry of workers.

- **Two leases, with one job each** (built, [#105](https://github.com/aadhithya/featherweight/issues/105)). The queue's lease keeps a *message* from other receivers, and it comes back by itself when the worker
  stops renewing it. The job's lease is on the job row (`jobs.worker_id`, `jobs.lease_expires_at`), and is what makes it *exclusive*:
  at-least-once delivery can deliver a message twice even without a lease running out, so the job needs a lock of its own. The default
  lease is 60 seconds (`JobWorker(lease=...)`). A job that runs in your process (`run_job`) has an owner and no lease: nothing renews one and
  nobody takes it over, so it has none to lose.
- **The heartbeat** (built, #105). One task per worker renews, every third of the lease (20 seconds; `JobWorker(heartbeat=...)`), the job lease of every job it runs
  (**one `UPDATE`, in place, no new rows**) and the queue lease of each of their messages (`extend`). It proves that the worker is alive,
  and not that a job makes progress: that is the timeout.
- **Takeover** (built, [#106](https://github.com/aadhithya/featherweight/issues/106)). A message comes back after its lease ended and reaches a worker. If the job is `RUNNING` and its lease has expired, the
  worker takes it over with one compare-and-set (`Database.take_over_job`): the job goes back to `INIT` (a `lost` event), and at once
  to `RUNNING` with the new owner and `executions` higher by one (a `running` event), in one transaction. If the lease has not
  expired (a duplicate delivery of a job that is alive), the worker acknowledges the message and leaves the job alone; so it does for a
  message for a job that **this worker** is running (its heartbeat can still renew a lease that merely expired, because renewing needs only the
  ownership). A job whose message never comes back is not found: the queue's redelivery is the only trigger, and there is no sweeper.
- **Executions.** `jobs.executions` counts how many times a worker started the job: 1 for the first, and one more each time it is started again (built,
  #105: a job that a shutdown put back in the queue is started again as execution 2; a takeover, #106, does the same). (It is not `attempt`, which is the
  retry of one agent call.) Each agent run records the execution that made it (`agent_runs.job_execution`, built, #106), and the job's span
  has `ftw.job.execution`. The execution comes from the task that runs the job (`execute_job` sets it, as it sets who the owner is), and not from the
  job's row, so that a worker that lost the job and has not noticed yet still records the execution it was.
- **A takeover reuses the steps that finished** (built, #106), if the job is `resumable`, with the same mechanism as a resume ([the runner](runner.md#resuming)): the
  source of the reuse is **this same job, executions before the current one**, and not another job. The results are already under this job's key, so
  nothing is copied forward. A job that is not resumable kept no results, and a takeover reruns it from the start.
- **The owner is checked on every write** (built, #105). The writes of a running job (renewing the lease, the final status and its event) are conditional on
  `worker_id` and `executions` being the worker's own, and on the job being `RUNNING`, in the same statement (`set_job_status(..., owner=JobOwner(...))`;
  `Database.owns_job` is the cheap read for the check before a result is written). A worker that was stalled and wakes up after a takeover finds
  that its write changes no row, which is how it learns that it lost the job. There is no token and no timer in it: the database
  decides, and the stalled worker's clock does not matter.
- **A lost job** (built, #105) is dropped quietly: the worker cancels the task at once (so that it stops spending), writes nothing to the job,
  does not acknowledge the message (the delivery is stale), logs `job_lost job_id=... execution=...`, and tells its observers (the job's span
  ends with error type `JobLost`, for tracing only: nothing is written to the job; its attempts end `CANCELLED`, as for any cancelled task). It keeps running its other jobs. Before the worker writes a result it checks that it still owns the
  job; that is not atomic, so in a very small window a stalled worker can write a result under the same key as the new owner, for the same
  input, and the last write wins. The effects that the job's own code already had (a mail sent, an API called) are not undone: **a job must be safe to run twice**.
- **The timeout** (built: [#107](https://github.com/aadhithya/featherweight/issues/107)). One execution of a job may run for `Job.timeout` seconds (default 600, `None` for no limit). When it is exceeded the
  task is cancelled and the job is `FAILED` with error type `JobTimeout`; it is not retried (the user resumes it). It applies to a job run
  by `run_job` too, because it is in the shared `execute_job`. Only the limit's own expiry counts: a `TimeoutError` that the job raises itself, and a
  cancellation from outside, do not (the job ends `FAILED` with that error, and `CANCELLED`). The agent runs that it interrupts end `CANCELLED`. A heartbeat cannot do this, because a hung job in a healthy worker still
  heartbeats.
- **Delivery count.** `Delivery.delivery_count` says how many times a message has been delivered. **How many redeliveries are allowed is the
  queue's business**: a broker has its own policy and dead-letter queue. Ours, `SqliteQueue`, allows 3 by default (`max_deliveries`), then marks
  the message `dead` and fails the job with error type `DeadLettered`. A user's queue that dead-letters a message ends its
  job with **`client.fail(job_id)`** (built, #106), from its dead-letter handler: a compare-and-set that moves a job that is not running to `FAILED`
  with error type `DeadLettered` (a `failed` event): one that is still `INIT`, or `RUNNING` with an expired lease (its worker was lost). It returns
  `False` for a job that runs with a live lease, is final, or does not exist. Until it is called, the job stays as it was. `SqliteQueue` calls it itself.

## Cancelling

- **A queued job** (`INIT`): `cancel` marks it `CANCELLED` (a `cancelled` event), in **one atomic step that succeeds only if the job is still `INIT`** (`Database.cancel_queued_job`). The worker takes a job with the matching step (`Database.start_job`: `RUNNING` only if still `INIT` and no one asked to cancel it). The two cannot both succeed, so a job is never cancelled and run at once. A cancel that loses falls through to a running job's flag. Its message stays on the queue, because brokers
  cannot remove by id; the worker sees the status when the message arrives, acknowledges it and skips it.
- **A running job:** `cancel` sets `cancel_requested` (a `cancel_requested` event). Inside the worker each running job is an asyncio
  task, so cancelling it is `task.cancel()`, which is event-driven. **In the same process**, `worker.cancel(job_id)` does it at once.
  **From another process**, one background task of the worker checks, about every second, which of **its own** running jobs have the
  flag, in one query, and cancels them. The job is then `CANCELLED` and its interrupted attempts are `CANCELLED`.
- **A final job:** `cancel` returns `False` and changes nothing.

## Job events

The worker, the client and `run_job` all change a job's status, and every change is an **event**, for anyone who wants to follow
a job. Events are metadata only: no content (content goes out only through [an observer](observability.md)).

```python
class JobEvent(BaseModel):  # frozen
    seq: int  # monotonic
    job_id: str
    type: Literal[
        "queued", "running", "succeeded", "failed", "cancelled", "cancel_requested", "lost"
    ]  # "lost": a takeover, see several workers
    status: RunStatus
    error_type: str | None
    error_hash: str | None
    at: datetime
```

**The database writes the event in the same transaction as the status change** (the outbox pattern), not the worker after it: a crash
cannot leave a change without its event, and every change makes exactly one event whoever makes it. `insert_job` writes `queued`,
`set_job_status` writes the event of the new status, and `request_job_cancel(job_id)` sets the flag and writes `cancel_requested`.
`list_job_events(job_id, after, limit)` reads them, and `poll_job_events(job_ids, after)` reads those of several jobs in one query (what the one reader asks for). These are methods of the `Database` abstract class, and its conformance kit covers them. `insert_job` writes `queued`, and a job going back to `INIT` writes `queued` again. After the commit the database **publishes** the event to the listeners in its own process.

**`client.events(job_id, after=0)`** is an async stream: it replays the stored events after a sequence number, then goes live, and
ends after a final event. A listener that starts late misses nothing, and one that reconnects resumes from its last `seq`.
`wait` reads the status and, if the job is not final, consumes the stream, so there is no gap between checking and listening.

**How it is delivered.** In the same process the database wakes the listeners when the transaction commits: instantly, with no
polling. Across processes the events table must be read, so **one reader task per process** polls it with a growing delay (one
query for every open stream) and fans out to them: a hundred open connections cost one query. The polling is inside the library,
in one place.

**SSE is the only streaming protocol.** `sse.encode(events)` turns the stream into server-sent events and `sse.HEADERS`
has the headers:

```
id: 12
event: running
data: {"seq":12,"job_id":"…","status":"RUNNING","at":"2026-11-20T10:00:01Z"}
```

`id` is the sequence number, so a browser that reconnects sends `Last-Event-ID` and the stream resumes after it. A comment line goes
out every 15 seconds to keep the connection open, and the stream ends after a final event. featherweight runs **no server**: the
user's API sends the response (a `StreamingResponse` in FastAPI, about a dozen lines, in the example). No WebSockets, one job per
stream, no stream of all jobs.

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
- **A lease on the queue (built, #104) and one on the job (built, #105):** the queue's keeps the message, the job's keeps the job exclusive. Heartbeats are
  in-place updates. A lost lease puts the job back to `INIT`, and `FAILED` comes only when the user's policy runs out.
- **A takeover is the same job with `executions` + 1,** not a new job: the client holds the id, and its events, its cancel and its result
  are all keyed by it. A resume is a new job; a takeover is not, and both reuse finished steps with one mechanism.
- **The write is checked against the owner,** not against a clock.
- **Job timeout, default ten minutes.** Redelivery limits are the queue's, except for ours.

## Later

- **An event channel as an abstract class,** like `Database`: Postgres `LISTEN/NOTIFY`, Redis pub/sub or a cloud broker would remove
  the polling across processes. Pub/sub is vendor-specific and not available everywhere, so we do not write one per vendor now.
  If it is wanted, it is one abstract class behind the same `events()`, with a conformance kit.
- **A worker-level limit on redeliveries:** the queue's business, so there is none.
- **Background agents:** [their own spec](background-agents.md), built on jobs and the job events.

## Not supported

- **A durable queue we ship** beyond sqlite, and a pub/sub channel.
- **Automatic retry of a failed job.**
- **A sweeper** for a job whose message never comes back.
- **A worker registry, scheduling, priorities, autoscaling,** and a broker of our own.
- **Fencing the user's stores and the effects of their code:** only our own writes are checked.
- **Progress with content** in events, and a stream of all jobs.
- **A server, a router or WebSockets.** The example shows an SSE endpoint.
- **Pruning events:** the table grows, and pruning is the user's for now.

## Tests

No live model call. Fake models, `MemoryQueue`, the dict result store.

- **The job class:** the contract, the hash, an agent as a job, a streaming agent refused, `@job`.
- **The queue:** `check_queue` against `SqliteQueue` and `MemoryQueue`, two receivers on one queue.
- **Events:** every status change makes exactly one event, whoever makes it; a late listener replays; the same-process wake-up; a
  writer and a reader on one file in two processes; one reader serving many streams; the stream ends at a final event; the SSE
  format and `Last-Event-ID`; the `Database` conformance kit on sqlite and Postgres.
- **Several workers:** `check_queue` with a short lease (built, #104: a message comes back with a higher count, `extend` keeps it, an acknowledged one
  does not, a stale delivery changes nothing); `SqliteQueue` with `max_deliveries` (dead, and its job `FAILED`); two workers, one killed mid-job: the other takes the job over,
  `executions` is 2 and the events read queued, running, lost, running, succeeded; a duplicate delivery of a live job is skipped; a stalled
  worker that wakes up loses its job quietly and writes nothing; the heartbeat keeps a long job alive; a takeover of a resumable job reuses its finished steps (and one that is not resumable reruns them), and
  never its own execution's; `client.fail` on an `INIT` job, and on a final one; the timeout fails a hung job (in the worker and in `run_job`).
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
| [#77](https://github.com/aadhithya/featherweight/issues/77) | The `@job` decorator |
| [#78](https://github.com/aadhithya/featherweight/issues/78) | The guide, an example and the skill reference |
| [#103](https://github.com/aadhithya/featherweight/issues/103) | The epic for several workers (see [several workers](#several-workers)), with #104 to #108 |
