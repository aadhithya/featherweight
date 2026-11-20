# Resuming a job

A release-notes job that fails halfway, and a second run that does not pay for the first half again. It shows
`resume_from`, and how a reused run appears in the trace. It uses `claude-haiku-5-5`, the smallest Claude model, with
thinking off, so a run costs next to nothing.

```
make example-resume
```

It needs `ANTHROPIC_API_KEY` in `.env`. Without Docker: `uv run python examples/resume/agent.py`.

**Where the results are kept.** A job that opts in with `resumable=True` keeps the output of each step in a *result store*. This
example does not pass one, so it uses the **default store**: a `results` table in the same sqlite file as the run log
(`SqliteResultStore`, which `AgentRunner` uses with a `SqliteDatabase`). That is why it needs no setup. To keep results
somewhere else (another database, a bucket, a store per tenant), write your own: [`examples/results`](../results) shows how in a
dozen lines, and with Postgres you have to, because there is no default store there.

## What happens

A `Summariser` summarises each of four commit messages (one `run_many`), then a `ReleaseNotes` agent writes the notes
from the summaries.

1. **First run.** The job is created `resumable=True`, so that it keeps the outputs of its steps. The job is made to
   raise after the summaries, as if the process had died. The job is `FAILED`, and the four summaries are in the run log
   with their tokens.
2. **Second run.** The same job, started with `resume_from=<the first job>`. It makes the same four summary calls,
   and each is answered from the first job's kept results: no model call, zero tokens, marked `reused`. Only the notes agent runs.

```
first run: the process dies after the summaries
  job FAILED: RuntimeError: simulated crash: the process died after the summaries
  summariser     SUCCESS  678 in / 69 out
  summariser     SUCCESS  677 in / 77 out
  summariser     SUCCESS  678 in / 73 out
  summariser     SUCCESS  670 in / 67 out

second run: the same job, with resume_from the first one
  job SUCCESS
  summariser     SUCCESS  0 in / 0 out   reused, no model call
  summariser     SUCCESS  0 in / 0 out   reused, no model call
  summariser     SUCCESS  0 in / 0 out   reused, no model call
  summariser     SUCCESS  0 in / 0 out   reused, no model call
  release_notes  SUCCESS  785 in / 259 out

Release notes: This release fixes a crash on unknown job IDs, adds a configurable retry delay, ...
```

That is a real run (the notes are cut short here). Token counts vary a little between runs. Each reused row has
`reused_from_agent_run_id` pointing at the row it came from, so the trace of the second job says where every result
came from.

## Things to try

- Change a prompt, or bump `version` of the `Summariser`, and run again: its agent hash changes, so nothing is reused and
  every summary is paid for again.
- Add a commit to `COMMITS`: the four old summaries are reused, and only the new one runs.
- `FTW_LOG_LEVEL=INFO make example-resume` also prints `run_reused` for each reused run.

## Where to look

[`agent.py`](agent.py) has the two agents and the job (about 100 lines). The rules, the limits and what is not
supported (a run launched by a model is never reused, and the code between agents must be deterministic) are in
[Resuming a job](../../docs/agents.md#resuming-a-job) and the [runner spec](../../docs/specs/runner.md).
