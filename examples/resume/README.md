# Resuming a job

A release-notes pipeline that fails halfway, and a second run that does not pay for the first half again. It shows
`resume_from`, and how a reused run appears in the trace. It uses `claude-haiku-5-5`, the smallest Claude model, with
thinking off, so a run costs next to nothing.

```
make example-resume
```

It needs `ANTHROPIC_API_KEY` in `.env`. Without Docker: `uv run python examples/resume/agent.py`.

## What happens

A `Summariser` summarises each of four commit messages (one `run_many`), then a `ReleaseNotes` agent writes the notes
from the summaries.

1. **First run.** The pipeline is made to raise after the summaries, as if the process had died. The job is `FAILED`, and
   the four summaries are in the run log with their tokens.
2. **Second run.** The same pipeline, started with `resume_from=<the first job>`. It makes the same four summary calls,
   and each is answered from the first job's rows: no model call, zero tokens, marked `reused`. Only the notes agent runs.

```
first run: the process dies after the summaries
  job FAILED: RuntimeError: simulated crash: the process died after the summaries
  summariser     SUCCESS  412 in / 71 out
  ... (four summaries)

second run: the same pipeline, with resume_from the first job
  job SUCCESS
  summariser     SUCCESS  0 in / 0 out   reused, no model call
  ... (four reused summaries)
  release_notes  SUCCESS  290 in / 140 out
```

This is an illustration: the token counts are made up, and yours will differ. Each reused row has `reused_from_agent_run_id` pointing at the row it came from, so the trace of
the second job says where every result came from.

## Things to try

- Change a prompt, or bump `version` of the `Summariser`, and run again: its agent hash changes, so nothing is reused and
  every summary is paid for again.
- Add a commit to `COMMITS`: the four old summaries are reused, and only the new one runs.
- `MICRO_LOG_LEVEL=INFO make example-resume` also prints `run_reused` for each reused run.

## Where to look

[`agent.py`](agent.py) has the two agents and the pipeline (about 100 lines). The rules, the limits and what is not
supported (a run launched by a model is never reused, and the code between agents must be deterministic) are in
[Resuming a job](../../docs/agents.md#resuming-a-job) and the [runner spec](../../docs/specs/runner.md).
