# Submitted jobs

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

A client submits a named, typed job. A separate worker takes it from a queue and runs it. The result goes to your result
store. featherweight keeps the status and the events of the job, never its input or its result.

Spec: [Submitted jobs](specs/submitted-jobs.md). Example: `examples/jobs` (`make example-jobs`).

- [The model](#the-model)
- [Defining a job](#defining-a-job)
- [Submitting and following](#submitting-and-following)
- [Who the job is for: the identity](#who-the-job-is-for-the-identity)
- [Cancelling](#cancelling)
- [Events and SSE](#events-and-sse)
- [Running a worker](#running-a-worker)
- [Several workers](#several-workers)
- [Your own queue](#your-own-queue)
- [What is not here](#what-is-not-here)

## The model

```text
JobClient --submit--> database (job row, "queued" event) + JobQueue --> JobWorker --> your ResultStore
    ^                                                                       |
    +---------------------- status, events, result -------------------------+
```

The client and the worker never talk to each other. The queue and the database are their only connection, so they can be two
processes, or two machines. A job is the same thing as in [Jobs](jobs.md): a row with a status. Submitting only adds
`queued` to it; the worker is what runs it.

## Defining a job

The example of this page, declared with the decorator ([jobs](jobs.md#declaring-a-job) says more):

```python
from featherweight import job, JobContext
from pydantic import BaseModel


class Text(BaseModel):
    text: str


class Shouted(BaseModel):
    shouted: str


@job
async def shout(ctx: JobContext, input: Text) -> Shouted:
    return Shouted(shouted=input.text.upper())
```

How to declare a job (`@job`, a `Job` class, the version, the timeout, `ctx.run`) is in [jobs](jobs.md#declaring-a-job). An agent is a job too: pass an agent class to `submit` and the worker runs it.

## Submitting and following

```python
client = JobClient(db, queue)  # results="default" uses the default result store
job_id = await client.submit(shout, Text(text="hello"))

await client.status(job_id)  # the JobRow
async for event in client.events(job_id):  # queued, running, succeeded
    ...
print(await client.result(shout, job_id))  # Shouted(shouted='HELLO'), read from your result store
```

The same job runs in your process with `run_job(runner, shout, Text(text="hello"))`: it writes the same row, events and result, and
returns the outcome. `result` is `None` until the job succeeded. `wait(job_id, timeout)` polls the database until the job is final: fine in
a script or a test, but a web handler should return the job id and let the caller follow `events`.

## Who the job is for: the identity

A tool on a worker can act for the user who started the job, as a tool in your process can ([who is asking](agents.md#who-is-asking-the-identity-of-a-run)).
`submit` takes `identity=`, the message carries it as JSON together with its kind (the name of its class), and the worker rebuilds it as the model that the kind names, from its own `identity_type`:

```python
runner = AgentRunner(db, identity_type=Teacher)
worker = JobWorker(db, queue, runner, jobs=[shout], identity_type=Teacher)
client = JobClient(db, queue, identity_type=Teacher)  # optional: it only checks the identity before a job is made

job_id = await client.submit(shout, Text(text="hello"), identity=Teacher(user_id="u1", tenant="uni"))
```

- **The worker's type is the authority.** A message without an identity, when the worker's type does not allow `None`, fails its job (`InvalidJobIdentity`),
  and so does an identity that does not fit the type, one whose kind the type does not have (a `Student`'s message for a worker that declares only `Teacher`), or one when the worker declares no type.
  The job is not run, and it is not retried. The error names the fields, never the values.
- **A worker can serve several kinds of people:** declare a union (`identity_type=Teacher | Student`) and the worker rebuilds each message as the model it was sent as, even when the models have the same fields.
  The kind is the name of the class, so a rename of an identity class while a job is queued fails that job, as a renamed job does. Two models of one union cannot have the same name.
- **The runner must accept the worker's type.** `JobWorker` raises `TypeError` when it is built if the runner's `identity_type` does not accept every type the worker rebuilds (and `None`, if the worker allows no identity): otherwise every job that runs an agent would fail. A worker may be stricter than its runner.
- **Fields marked `exclude=True` are not sent,** so a value that exists only in your process stays there. The worker's copy is equal to the original without them.
- **A job that an agent starts in the background on a worker** (a runner with `background=JobClient`) is submitted with the identity of its parent.
- **It is data in the queue message,** until the message is acknowledged: the queue is trusted, so keep the identity small and keep secrets out of it, and secure the queue the way you secure the
  input. A tool on a worker gets a credential from the identity with its own code; featherweight has no provider for it. The [identity spec](specs/identity.md) has the rules and what is yours.

## Cancelling

```python
await client.cancel(job_id)  # False if the job is already final, or does not exist
```

A queued job becomes `CANCELLED` at once, and the worker skips its message. A running job is asked to stop: the worker sees
the request within about a second (at once when the worker is in the same process), and the job ends `CANCELLED`. The client and the worker race for
a queued job, and exactly one of them wins: a job is never both cancelled and run.

## Events and SSE

`client.events(job_id, after=n)` is the stream of the job's events that [jobs](jobs.md#following-a-job-events-and-sse) describes (`job_events`): the stored ones after `n`, then each new one, ending at the final one. A worker writes the same events as `run_job` does. To send them to a browser, `sse.encode(client.events(job_id, after=after))` with `sse.HEADERS`, as in that section; `examples/jobs/api.py` is this behind FastAPI.

## Running a worker

```python
worker = JobWorker(db, queue, AgentRunner(db), jobs=[shout], concurrency=10)
await worker.run()  # until stop() or cancellation
await worker.stop(grace=30)
```

- It runs only the jobs you list (and the ones your `jobs_lookup` finds, [below](#jobs-that-are-not-declared-in-code)); a message for another name fails that job.
- It never retries a failed job. If the job was `resumable`, you resume it with a new job ([Persistence](persistence.md)).
- The result is written to your result store, keyed by the job id, the job hash and the input hash. If it cannot be written, the job fails.
- While it runs jobs, the worker holds a **lease** on each (`JobWorker(lease=60)`, in seconds) and renews it, and the queue's lease of the message, every third of
  it (`heartbeat=`). A worker that finds a job taken from it stops that job at once and writes nothing more to it. A job must be safe to run twice.
- On `stop`, the worker takes no new messages, waits up to `grace` seconds, then cancels what is left, puts those messages back on the queue and
  sets those jobs back to `INIT`, so they run again.

### Jobs that are not declared in code

A worker is given its jobs when it starts. If your users define their own agents (kept in your database, built into classes when they are needed), a worker
cannot have been given them. `jobs_lookup` is a **fallback** for those: `jobs` is tried first, and the lookup is called only for a name `jobs` does not have.

```python
async def jobs_lookup(name: str, job_hash: str | None, identity: Teacher | None):
    definition = await my_agents.get(identity.tenant, name)  # your storage; the identity can scope the search
    return build_agent(definition) if definition else None  # an agent class or a Job class; None: unknown


worker = JobWorker(
    db, queue, AgentRunner(db, identity_type=Teacher), jobs=[], jobs_lookup=jobs_lookup, identity_type=Teacher
)
```

- It gets the name from the message, the `job_hash` recorded when the job was submitted, and the identity the worker rebuilt (an application that does not need it ignores it).
- The worker **checks what it returns** before anything runs: the name must be the one asked for, and `job_hash()` must be the recorded hash. If the user edited
  their agent while the job waited, the job fails (`JobLookupError`) instead of running the new definition. A lookup that keeps old versions of a definition can use `job_hash` to load the exact one.
  `None` is `UnknownJob`; a lookup that raises, or returns something that is not a job, fails the job with `JobLookupError`. The worker never retries a job, so a lookup that can fail for a
  transient reason retries inside itself.
- The worker caches nothing. The hash is made from the content, so you may cache what you built by `(name, job_hash)`.
- **Names are yours.** A job's name is a string you choose, unique in what your lookup searches (with the identity, unique within a tenant is enough) and stable: a rename breaks the jobs queued
  under the old name. It must match `[A-Za-z][A-Za-z0-9_.-]*`.
- On the client side nothing changes: `submit` takes the class that you built from the same definition.

The [spec](specs/submitted-jobs.md#jobs-that-are-not-declared-in-code) has the rules. It is not a registry: where definitions live, who may define one and which tools a user may pick are your application's.

## Several workers

Run as many workers as you like on one queue: a message goes to one of them, and one wins the job. (`SqliteQueue` is for one machine; for
several, share one database (`PostgresDatabase` ships in the package) and use a queue of your own: [your own queue](#your-own-queue).)

- **A worker that dies.** While a worker runs a job it renews two leases about every 20 seconds (a third of `lease`, 60 seconds by default): the
  job's, in the database, and its message's, in the queue. If the worker dies, the leases run out. The message comes back to another worker,
  which sees that the job's lease expired and **takes it over**: the same job, with `executions` one higher and a `lost` event in its
  events (queued, running, lost, running, succeeded). A message that arrives while the job's lease is still alive is a duplicate: it is acknowledged and the job is left alone.
- **What the new worker reuses.** If the job is `resumable`, the steps that finished before the worker died are reused, as in a
  [resume](jobs.md#resuming-a-job); otherwise the job runs again from the start. A resume is you running a job that *ended* again, as a new
  job; a takeover is the system running a job that *did not end* again, as the same job.
- **A worker that only stalled.** If a worker was paused (not dead) and wakes up after a takeover, its writes are refused, because every write of a running
  job checks that the worker still owns it. It stops the job at once and writes nothing more. Only featherweight's writes are checked: **a job must be safe
  to run twice**, because what its own code already did (a mail sent, an API called) is not undone.
- **A job that hangs.** The heartbeat only shows that a worker is alive. Each job has a `timeout` (`@job(timeout=...)`, 600 seconds by default, `None` for no limit):
  when it passes, the job is cancelled and fails with error type `JobTimeout`. It is not retried; you resume it.
- **How often a message may come back** is your queue's policy. `SqliteQueue(db, lease=60, max_deliveries=3)` marks a message `dead` after three deliveries and
  fails its job (`DeadLettered`). If your own queue dead-letters a message, call `client.fail(job_id)` from its dead-letter handler to end the job.

`examples/jobs/takeover.py` does it: it kills a worker with SIGKILL in the middle of a job, and another one takes the job over (`make example-takeover`).

## Your own queue

`SqliteQueue` is the only queue that featherweight ships, for local work. For production, subclass `JobQueue`
(`enqueue`, `receive`, `ack`, `nack`, `extend`) over the queue you run, and check it with the kit. The kit gives your factory the lease in seconds
(a short one, to see a message come back):

```python
await testing.check_queue(lambda lease: MyQueue(visibility_timeout=lease))
```

**The lease.** A received message is yours for a limited time. If you do not `ack` it, `nack` it or `extend` it before the lease ends, it is
delivered again to another receiver, and its `Delivery.delivery_count` is higher. `extend` renews the lease; a delivery that has been replaced
(a stale one) must change nothing. You do not have to build it: a broker has it. On SQS the lease is the visibility timeout
(`extend` is `ChangeMessageVisibility`, and the delivery count is `ApproximateReceiveCount`); on a Redis stream it is the pending list
(`XAUTOCLAIM` takes a message over, `extend` is a `XCLAIM` of your own entry to reset its idle time, and the delivery counter is in `XPENDING`).
How many times a message may be delivered is your broker's policy, with its own dead-letter queue. The kit asks that a message can be delivered at
least three times. `SqliteQueue(db, lease=60, max_deliveries=3)` is the one we ship: after `max_deliveries` it marks the message `dead`.

A message also carries a `trace_context` (a small mapping of strings, empty unless the client has an observer that gives one) and an `identity` (a JSON mapping, or `None`) and its `identity_kind` (a string, or `None`: [who the job is for](#who-the-job-is-for-the-identity)): carry them
unchanged, and the kit checks that you do. A queue that drops the identity or its kind fails the kit.

A dead-letter queue is yours to configure on your queue: a message that is `nack`ed without `requeue` is yours to keep or drop.

## What is not here

- A worker registry, a scheduler, priorities, and a sweeper for a job whose message never comes back.
- An event channel such as pub/sub: events are read from the database for now.
