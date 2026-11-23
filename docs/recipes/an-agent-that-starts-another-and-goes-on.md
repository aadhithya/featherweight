# An agent that starts another and goes on

```python
class Researcher(AgentBase):
    background_jobs = [WebSearcher]  # the model gets `start_background`; the child is a job


runner = AgentRunner(db, results=store)  # here, in this process; needs a result store
runner = AgentRunner(db, results=store, background=client)  # or on a worker: a JobClient
message = await runner.background_message(job_id)  # a user message with the child's result, for the next turn
```

While the parent runs, a finished child's result is added before the next model call. A chat turn does not wait: follow `background_started` and
`background_finished` on its stream, or the child's job events, and start the next turn yourself. [the background jobs guide](../background-agents.md) has the rest.
