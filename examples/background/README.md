# A background agent that wakes the chat

A chat agent starts a researcher **in the background** and goes on talking. When the researcher is done, the chat answers again,
without the user typing anything. It uses `claude-haiku-5-5`, the smallest Claude model, with thinking off. It needs
`ANTHROPIC_API_KEY` in `.env`.

```
make example-background
```

Ask it to research something ("research how kettles work"), then keep chatting. Without Docker:
`uv run python examples/background/session.py`.

[`session.py`](session.py) has the two agents and a `Session`:

- the chat agent lists the researcher in `background_jobs`, so it gets a `start_background` tool that returns at once;
- featherweight emits a `background_started` event on the chat stream, with the id of the child's job;
- **the `Session` is the part that wakes the chat.** It follows that job's events, and when it ends it starts the next turn with the
  history plus `await runner.background_message(job_id)`, which is the researcher's note as a user message.

featherweight itself wakes nothing, and keeps no conversation: the `Session` is about 25 lines, and yours to copy and change. Its state is in
memory, so **a restart loses a result that has not been delivered yet**. Keep it where you need it (the job's result is in your result store;
here that is the in-memory database).

The researcher runs in this process. To run it on a worker, give the runner a `JobClient`
(`AgentRunner(db, background=client)`) and a worker that lists the job: nothing else changes ([the guide](../../docs/background-agents.md) says how).
