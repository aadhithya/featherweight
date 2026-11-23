# A job that a worker runs

```python
@job
async def shout(ctx: JobContext, input: Text) -> Shouted:
    return Shouted(shouted=input.text.upper())


queue = SqliteQueue(db)
worker = JobWorker(db, queue, AgentRunner(db), jobs=[shout])  # its own process: await worker.run()
client = JobClient(db, queue)
job_id = await client.submit(shout, Text(text="hello"))
async for event in client.events(job_id):
    ...  # or sse.encode(...) from a web handler
print(await client.result(shout, job_id))
```

Run as many workers as you like: a worker that dies loses its lease, and another takes the job over (`executions` + 1; a resumable job reuses its finished steps).
A job must be safe to run twice, and `@job(timeout=...)` (600 s by default) stops one that hangs. [the submitted jobs guide](../submitted-jobs.md) has cancelling, shutdown, leases and writing your own queue. `examples/jobs` runs it, and `takeover.py` kills a worker mid-job.
