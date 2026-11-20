# Submitting jobs

A client submits a job, a separate worker runs it, and the client follows it. No model and no API key: the job is plain code
(`shout.py`), so you can see how little it takes.

```
make example-jobs
```

[`run.py`](run.py) submits a job and runs a worker for it in one script, and prints the events as they happen (`queued`,
`running`, `succeeded`) and the result. Without Docker: `uv run python examples/jobs/run.py`.

The client and the worker only meet in the queue and the database, so they can be two processes:

```
uv run python examples/jobs/worker.py                      # a job worker
uv run uvicorn --app-dir examples/jobs api:app             # an API that submits, follows (SSE) and reads
curl -X POST localhost:8000/jobs -H 'content-type: application/json' -d '{"text": "hello"}'
curl -N localhost:8000/jobs/<job id>/events                # server-sent events, until the job is done
curl localhost:8000/jobs/<job id>/result
```

A worker can die in the middle of a job. [`takeover.py`](takeover.py) kills one with SIGKILL while it holds a job, and another worker takes the job
over once the lease runs out (the events read `queued`, `running`, `lost`, `running`, `succeeded`):

```
make example-takeover
```

[`shout.py`](shout.py) is the job: `@job` over an async function with a Pydantic input and output. The default queue and result
store are sqlite, which is for local development; in a deployment you give the worker and the client a queue and a result store
of your own (see the [guide](../../docs/submitted-jobs.md)).
