# Resuming a failed job

Create the first job with `resumable=True` (it keeps the outputs of its steps), and when it fails, run the same job again
and point it at the failed job:

```python
first = await run_job(runner, find_people, texts, resumable=True)
outcome = await run_job(runner, find_people, texts, resume_from=first.job_id)
```

Agent runs that succeeded with the same agent and input are answered from the kept results (`result.reused`, zero tokens). Keep
the code between agents deterministic (no clock, no `random`), and do not rely on state outside the result store for a resume.
