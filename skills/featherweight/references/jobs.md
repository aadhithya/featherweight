<!-- Copied from docs/jobs.md by scripts/build_skill.py. Edit the original, not this file. -->

# Jobs

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

A job is a pipeline with a name, an id, a status and events. How to declare one, run it in your process, follow it, and resume it after a failure. To run a job on a worker from a queue, see [submitted jobs](submitted-jobs.md); what the run log keeps, and where results are stored, is in [persistence](persistence.md); an agent that starts a job and goes on is in [background jobs](background-jobs.md); traces are in [observability](observability.md). Agents are in [agents.md](agents.md).

- [Declaring a job](#declaring-a-job)
- [Running a job](#running-a-job)
- [Following a job: events and SSE](#following-a-job-events-and-sse)
- [Resuming a job](#resuming-a-job)

## Declaring a job

A job is a declared class, like an agent (`@job`, below, builds one from a function):

```python
class Shout(Job):
    name = "shout"
    version = "1"
    input_model = ShoutInput  # Pydantic
    output_model = ShoutOutput

    async def run(self, ctx: JobContext, input: ShoutInput) -> ShoutOutput:
        result = await ctx.run(Shouter, ShouterInput(text=input.text))  # an agent, run in this job
        ...
```

The name, the version and both models are checked when the class is defined, and `Shout.job_hash()` changes when any of them (or
either schema) does. `ctx.run` and `ctx.run_many` run agents **in the job**, so they share its trace and its resume. A plain
agent can be a job too (`as_job(Shouter)`: its name, its models, and its `agent_hash`); a streaming agent cannot.

For a job that is just a function, `@job` builds the class from it, reading the models from the annotations:

```python
@job  # or @job(name="shout", version="2", timeout=120)
async def shout(ctx: JobContext, input: ShoutInput) -> ShoutOutput: ...
```

The name is the function's, the version `"1"` (the version of the job is the version of the pipeline: change it when its
behaviour changes), the timeout 600 seconds (a job that runs longer fails with `JobTimeout`; `timeout=None` for no limit), and the result is a `Job` class, usable wherever one is (the function is `shout.function`).

## Running a job

A **job** is a pipeline declared as a class with typed input and output, like an agent. It runs in your process with `run_job`, or on a worker ([submitted jobs](submitted-jobs.md)), and it is the same job either way. `run_job` creates it, marks it `RUNNING`,
runs it and marks it `SUCCESS`, or `FAILED` with the error. A job that raises comes back as a failed `JobOutcome`, not an exception.

```python
from featherweight import AgentRunner, JobContext, job, run_job


@job
async def my_pipeline(ctx: JobContext, input: PipelineInput) -> PipelineOutput:
    return PipelineOutput(...)  # plain async Python: ctx.run and ctx.run_many run agents in this job


outcome = await run_job(AgentRunner(db), my_pipeline, PipelineInput(...))
# outcome.status, outcome.result (the output model, on success), outcome.error ("ExceptionType: message", on failure)
```

The output is also written to the runner's result store if it has one (`results=None` keeps nothing; you still get it back). To
run in the background and hand the caller the id first, create the job with `db.create_job` and use
`asyncio.create_task(run_job(runner, my_pipeline, input, job_id=job_id))`. For a quick try, `SqliteDatabase(":memory:")` keeps
everything in memory: no file, no service.


## Following a job: events and SSE

Every change of a job's status is an **event** (`queued`, `running`, `succeeded`,
`failed`, `cancelled`, `cancel_requested`, and `lost`: the job's worker was lost and the job went back to the queue to be taken over), and the database writes it **in the same transaction** as the change, whoever
makes it (`run_job`, a client, a worker). An event is metadata only (`seq`, `job_id`, `type`, `status`, the error's type and hash,
`at`). `job_events(db, job_id, after=0)` streams them: the stored ones first, then each as it happens, ending after a final
one. A stream that starts late replays what it missed, and one that reconnects passes the `seq` of the last event it had.

```python
async for event in job_events(db, job_id):
    print(event.type)  # queued, running, succeeded
row = await wait_for_job(
    db, job_id, timeout=60
)  # the same stream, until the job is final (a convenience for scripts and tests)
```

In the process that wrote an event, the listener hears it at commit, with no polling. From another process, the table is read by
**one reader task per `Database` object**, with a short, growing delay, for every open stream together: a hundred open streams
cost one query, not a hundred. Polling is in that one place.

**SSE** is the one way events are streamed to a client. `sse.encode` turns the stream into server-sent events
(`id` is the `seq`, so `Last-Event-ID` resumes; a comment goes out every 15 seconds to keep the connection open) and
`sse.HEADERS` has the headers. featherweight runs no server: your API sends the response.

```python
@app.get("/jobs/{job_id}/events")
async def events(job_id: str, request: Request):
    after = int(request.headers.get("last-event-id", 0))
    return StreamingResponse(sse.encode(job_events(db, job_id, after)), headers=sse.HEADERS)
```

## Resuming a job

A pipeline fails at step 2. Run it again with `resume_from`, and step 1 is not paid for twice. **The first job has to opt in
before it runs**, because featherweight keeps no outputs unless you ask:

```python
first = await run_job(runner, my_pipeline, PipelineInput(...), resumable=True)  # fails at step 2
outcome = await run_job(runner, my_pipeline, PipelineInput(...), resume_from=first.job_id)
```

- **`resumable=True`** keeps the output of each step of the job, in a **result store**. Without it nothing is kept, and
  `resume_from` that job raises `ValueError` ("it did not keep its results"), before any model call. To run everything
  again, run the pipeline without `resume_from`.
- **The result store** is where the outputs are kept: `AgentRunner(db, results=...)`. The default is a `SqliteResultStore`, a
  table in the run log's sqlite file, which is for local development. `results=None` keeps nothing, and then resuming a job
  is a `SetupError`. To keep outputs somewhere else (a database per tenant, a bucket), write a [`ResultStore`](persistence.md#a-result-store-of-your-own).
- **Resuming is up to you.** Nothing retries a failed job by itself. The attempts of one run are retried
  automatically (`max_attempts`), but a failed job is resumed by the person or the code that decides to, for example a
  retry button that calls `run_job(..., resume_from=...)`.

This creates a **new job** that records `resumed_from`, and is resumable itself, so that a third attempt is possible. Your
pipeline runs again from the top, with its own loops and decisions. Only the runs are answered from kept results. It is
replay, not pausing: featherweight is not a workflow engine. To create the job first, use `db.create_job(name,
resume_from=old_job_id)` and pass its id with `job_id` (`run_job` refuses `job_id` together with `resume_from` or
`resumable`). [`examples/resume`](https://github.com/aadhithya/featherweight/blob/main/examples/resume) runs a pipeline that crashes halfway and resumes, with the default result store and Claude Haiku
(`make example-resume`).

For each run that your pipeline starts in the new job, the runner looks in the old job for a row that

- has status `SUCCESS`,
- has the same `agent_hash` (the same agent: prompt, version, model settings, tools and so on),
- has the same input (`input_hash`),
- has the same identity (`identity_hash`, made from the kind of the identity, the name of its class, and its fields: [who the run is for](agents.md#who-is-asking-the-identity-of-a-run); a run with none matches only a run with none), and
- is a top-level run (`depth` 1).

A job is for one identity, and a resume is a new job: to resume a user's job as someone else (a support person), give the new job their identity, and every step reruns.

If there is one, the runner gets its output from the result store, and checks that it still validates against the output model
and passes `check_output`. If so, it does not call the model and takes no concurrency slot. It writes a `SUCCESS` row of its own in
the new job, with zero tokens, the kept output and `reused_from_attempt_id`, copies the result to the new job (so deleting the old job
never affects it), and returns an `AgentResult` with
`reused=True` and zero tokens. The log says `run_reused ...`. If the output no longer validates, the log says
`run_reuse_rejected reason=...` and the agent runs as usual. If the store has no result for a step that the log says
succeeded (it was never kept, or was lost), the log says `result_missing` and the agent runs. A store that fails to keep a
result never fails the run: that step just cannot be reused later. With several matches (the same input twice in one
`run_many`), any one answers. `run_many` resumes per input: one failed section of ten reruns only that section.

Changing the prompt, bumping `version` or changing a model setting changes `agent_hash`, so that step runs again, and so
does any step whose input depends on its output.

What to know:

- **The code between agents must be deterministic**: no clock, no `random`, no unordered iteration. It is your code, so
  it is your responsibility, and featherweight does not check it. If it takes another path, the agents get other inputs,
  nothing matches, and they run again.
- **State outside the result store is not kept.** Outputs come from the result store. A reused step does not run, so
  anything it kept in memory (a counter, a dict) is not rebuilt. Do not rely on it for anything a resume needs.
- **Side effects are not undone or repeated.** A tool that sent an email in the failed job is not rerun when its agent's
  result is reused. An agent that failed halfway runs whole again.
- **Rows from before `agent_hash` existed are never reused**, and neither are `FAILED`, `INIT` and `RUNNING` rows.
- **Depth 1 only.** A run launched by a model (a sub-agent) is never looked up. A parent that succeeded is reused whole,
  and its children never launch. A parent that failed runs again, and so do its children. Reusing children is not
  included.
- **A crash inside your pipeline code** runs that code again, and its agent calls are answered from the log.
