# Submitted jobs

A client submits a named, typed job. A separate worker takes it from a queue and runs it. The result goes to your result
store. featherweight keeps the status and the events of the job, never its input or its result.

Spec: [Submitted jobs](specs/submitted-jobs.md). Example: `examples/jobs` (`make example-jobs`).

- [The model](#the-model)
- [Defining a job](#defining-a-job)
- [Submitting and following](#submitting-and-following)
- [Cancelling](#cancelling)
- [Events and SSE](#events-and-sse)
- [Running a worker](#running-a-worker)
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
job worker runs it. Inside a job, `await ctx.run(SomeAgent, input)` and `ctx.run_many(...)` run agents in the same job.

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
- On `stop`, the worker takes no new messages, waits up to `grace` seconds, then cancels what is left, puts those messages back on the queue and
  sets those jobs back to `INIT`, so they run again.

## Your own queue

`SqliteQueue` is the only queue that featherweight ships, for local work. For production, subclass `JobQueue`
(`enqueue`, `receive`, `ack`, `nack`) over the queue you run, and check it with the kit:

```python
await testing.check_queue(lambda: MyQueue())
```

A message also carries a `trace_context` (a small mapping of strings, empty unless the client has an observer that gives one): carry it
unchanged, and the kit checks that you do.

A dead-letter queue is yours to configure on your queue: a message that is `nack`ed without `requeue` is yours to keep or drop.

## What is not here

- Several workers on one queue (leases, heartbeats, a worker that dies): a later epic.
- Agents that run in the background: a later epic.
- An event channel such as pub/sub: events are read from the database for now.
