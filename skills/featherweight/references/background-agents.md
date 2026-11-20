<!-- Copied from docs/background-agents.md by scripts/build_skill.py. Edit the original, not this file. -->

# Background agents

An agent hands work to another agent and **goes on**. A launched sub-agent ([launching agents](launching-agents.md)) blocks its parent until it
returns. A background agent does not: the agent starts it, gets a job id at once, and the result comes back when it is ready.

Spec: [Background agents](https://github.com/aadhithya/featherweight/blob/main/docs/specs/background-agents.md). Example: `examples/background` (a session that wakes itself).

- [How it works](#how-it-works)
- [Declaring it](#declaring-it)
- [Where it runs](#where-it-runs)
- [Getting the result back](#getting-the-result-back)
- [Events](#events)
- [A chat in the browser](#a-chat-in-the-browser)
- [How long a child lives](#how-long-a-child-lives)
- [Limits](#limits)
- [What is not here](#what-is-not-here)

## How it works

A background agent is **a job that is exposed to an agent as a tool**. It has a job id, events, a result in your store, and it can be cancelled,
as any [job](submitted-jobs.md). featherweight wakes nothing: it emits events, and your application decides what to do with them. The model
never polls.

## Declaring it

```python
from featherweight import AgentBase


class Researcher(AgentBase):
    background_agents = [WebSearcher, Summariser]  # agent classes, or job classes
```

- The model gets one more tool, `start_background(agent_name, input_json)`. It returns at once with `{"status": "STARTED", "agent": ..., "job_id": ...}`,
  and, like `launch_agent`, it never raises into the parent's run: a wrong name, an input that does not fit or the limit gives a sentence the model can read.
- An agent may be in `subagents` too. Then the model chooses, per call, between `launch_agent` (it waits and gets the output) and `start_background`
  (it goes on). The prompt describes each agent once, as inline, background or either.
- A streaming agent cannot be a background agent. A background agent cannot start background agents.

## Where it runs

The same job, in your process or on a worker, by configuration:

```python
runner = AgentRunner(db)  # here: an asyncio task in this process (the default)
runner = AgentRunner(
    db, background=client
)  # on a worker: `client` is a JobClient, and the child is submitted to its queue
```

Either way the child has the same job row, events and result. On a worker, the worker must list the child's job in its `jobs`. The runner needs a
**result store** to read the result from (`results=`, or the client's): an agent with `background_agents` on a runner without one is a `SetupError`, before any model call.

## Getting the result back

The tool has already returned ("started"), and a second result for the same call would be rejected by the providers. So when the child finishes, its
result arrives as **new information**, a user message:

```
<background_result job_id="…" agent="Summariser" status="SUCCESS">
{ the output, as JSON }
</background_result>
```

A failed child has `status="FAILED"` and its `error_type` (never its error text), and a cancelled one `status="CANCELLED"`. There is no output.

- **While the parent runs,** the message is added before the next model call, after the results of the round's tool calls, once for each child.
- **After a chat turn has ended,** it is yours: start the next turn with the history and the message. `await runner.background_message(job_id)`
  builds it from the job's status and your result store (a LangChain `HumanMessage`).

```python
history = [*history, await runner.background_message(job_id)]  # then start a turn with it, as for any message
```

## Events

- **On a chat stream** (a streaming agent, while the stream is open), two AG-UI `CUSTOM` events: `background_started` (`job_id`, `agent`) and
  `background_finished` (`job_id`, `agent`, `status`, `error_type`). They carry metadata only, never the output.
- **The child's own job events** (`queued`, `running`, `succeeded`, ...) are the full record. Follow them with `client.events(job_id)`, and over SSE with
  `sse.encode`, as for any job ([submitted jobs](submitted-jobs.md#events-and-sse)). The chat events are shortcuts for the chat connection,
  derived from them. After a chat stream has ended, follow the job's events yourself: the job id is in `background_started`.

## A chat in the browser

The AG-UI adapter passes the events through. The Vercel adapter sends them as **transient data parts**, `data-background_started` and
`data-background_finished`: the `useChat` hook gives them to `onData`, and does not keep them in the messages.

```tsx
const [children, setChildren] = useState<Record<string, string>>({});  // job id -> status

const { messages, sendMessage } = useChat({
  onData: (part) => {
    if (part.type === "data-background_started") setChildren((c) => ({ ...c, [part.data.job_id]: "running" }));
    if (part.type === "data-background_finished") setChildren((c) => ({ ...c, [part.data.job_id]: part.data.status }));
  },
});
```

Show a chip for each running child, and when one finishes, call your own endpoint: it calls `runner.background_message(job_id)` and starts the next turn.
The type of a data part depends on the version of the SDK (check how it types `onData`); the shape that is sent is the one above.

## How long a child lives

- **A chat turn does not wait.** It ends when the model ends it, and its children carry on. If the user **stops** the response, its children are cancelled.
- **A run that is not a chat waits.** When the model would end its run while a child still runs, the run waits for it, adds its result, and the model
  goes on (within `recursion_limit`). Cancelling the run cancels its children.
- A cancelled or failed child is a result like any other: the parent is told, and the model decides.
- featherweight keeps no pending result for a parent whose turn is over. If your process restarts, what you did not store is lost; a child on a worker
  carries on, and its result is in your store.

## Limits

- **Live children:** `AgentRunner(max_background=3)` is the most children one attempt of one agent may have running. A start over the limit is refused with a sentence.
- A background agent is a job of its own, so `max_depth` and `max_launches` count within it.

## What is not here

- **A session that wakes itself.** It is not part of the framework: it is a small class in your application (the example has one, about 40 lines).
- Arbitrary statuses streamed by a background agent ("searching 3 of 5 sources"), a tool for the model to wait for or poll a child, a durable inbox,
  and background agents that start background agents.
