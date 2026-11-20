# Submitted jobs

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

A client submits a named, typed job. A separate worker takes it from a queue and runs it. The result goes to your result
store. featherweight keeps the status and the events of the job, never its input or its result.

Spec: [Submitted jobs](specs/submitted-jobs.md). Example: `examples/jobs` (`make example-jobs`).

- [The model](#the-model)
- [Defining a job](#defining-a-job)
- [Submitting and following](#submitting-and-following)
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
processes, or two machines. A job is the same thing as in [Agents](agents.md): a row with a status. Submitting only adds
`queued` to it; the worker is what runs it.

## Defining a job

A job has a name, an input model, an output model and a `run`. The decorator is the short way:

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

The name is the function name, and the models are read from the annotations. For more control, subclass `Job` and set
`name`, `version`, `input_model` and `output_model` yourself. An agent is a job too: pass an agent class to `submit` and the
job worker runs it. A job may run for `timeout` seconds (default 600, `None` for no limit): set `timeout = 120` on the class, or `@job(timeout=120)`.
A job that runs longer is cancelled and fails with error type `JobTimeout`, in a worker and in `run_job` alike. Inside a job, `await ctx.run(SomeAgent, input)` and `ctx.run_many(...)` run agents in the same job.

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

## Cancelling

```python
await client.cancel(job_id)  # False if the job is already final, or does not exist
```

A queued job becomes `CANCELLED` at once, and the worker skips its message. A running job is asked to stop: the worker sees
the request within about a second (at once when the worker is in the same process), and the job ends `CANCELLED`. The client and the worker race for
a queued job, and exactly one of them wins: a job is never both cancelled and run.

## Events and SSE

Every status change writes an event in the same transaction, so the stored events are the history and cannot disagree with the
status. `events(job_id, after=n)` yields the stored events after `n`, then each new one, and ends at the final one.

SSE is the only streaming protocol. featherweight runs no server: you return the stream from your own handler.

```python
@app.get("/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    after = int(request.headers.get("last-event-id", 0))  # a browser that reconnects resumes where it was
    stream = sse.encode(client.events(job_id, after=after))
    return StreamingResponse(stream, headers=sse.HEADERS)
```

Each event is `id: <seq>`, `event: <type>` and `data: <json>`, with a keep-alive comment every 15 seconds. `examples/jobs/api.py`
is this behind FastAPI.

## Running a worker

```python
worker = JobWorker(db, queue, AgentRunner(db), jobs=[shout], concurrency=10)
await worker.run()  # until stop() or cancellation
await worker.stop(grace=30)
```

- It runs only the jobs you list; a message for another name fails that job.
- It never retries a failed job. If the job was `resumable`, you resume it with a new job ([Persistence](persistence.md)).
- The result is written to your result store, keyed by the job id, the job hash and the input hash. If it cannot be written, the job fails.
- While it runs jobs, the worker holds a **lease** on each (`JobWorker(lease=60)`, in seconds) and renews it, and the queue's lease of the message, every third of
  it (`heartbeat=`). A worker that finds a job taken from it stops that job at once and writes nothing more to it. A job must be safe to run twice.
- On `stop`, the worker takes no new messages, waits up to `grace` seconds, then cancels what is left, puts those messages back on the queue and
  sets those jobs back to `INIT`, so they run again.

## Several workers

Run as many workers as you like on one queue: a message goes to one of them, and one wins the job. (`SqliteQueue` is for one machine; for
several, share one database (`PostgresDatabase` ships in the package) and use a queue of your own: [your own queue](#your-own-queue).)

- **A worker that dies.** While a worker runs a job it renews two leases about every 20 seconds (a third of `lease`, 60 seconds by default): the
  job's, in the database, and its message's, in the queue. If the worker dies, the leases run out. The message comes back to another worker,
  which sees that the job's lease expired and **takes it over**: the same job, with `executions` one higher and a `lost` event in its
  events (queued, running, lost, running, succeeded). A message that arrives while the job's lease is still alive is a duplicate: it is acknowledged and the job is left alone.
- **What the new worker reuses.** If the job is `resumable`, the steps that finished before the worker died are reused, as in a
  [resume](persistence.md#resuming-a-job); otherwise the job runs again from the start. A resume is you running a job that *ended* again, as a new
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

A message also carries a `trace_context` (a small mapping of strings, empty unless the client has an observer that gives one): carry it
unchanged, and the kit checks that you do.

A dead-letter queue is yours to configure on your queue: a message that is `nack`ed without `requeue` is yours to keep or drop.

## What is not here

- A worker registry, a scheduler, priorities, and a sweeper for a job whose message never comes back.
- An event channel such as pub/sub: events are read from the database for now.
