# Spec: background agents

Status: designed, not built. It is tracked in [epic #115](https://github.com/aadhithya/featherweight/issues/115), and each part has its own issue (listed [at the end](#the-work)). When a part is
built, this page says so, and the guide `docs/background-agents.md` shows how to use it.

It builds on [submitted jobs](submitted-jobs.md) (a job is a declared class that runs in your process or on a worker, with events and a
result in your store), [the runner](runner.md#launching-agents) (launching agents, and its limits) and [streaming agents](streaming.md)
(the events of a chat run). What it changes in them is in them, and not here ([what changes](#what-changes)).

- [Goal](#goal)
- [The principle](#the-principle)
- [The tool](#the-tool)
- [Where it runs](#where-it-runs)
- [Getting the result back](#getting-the-result-back)
- [Events](#events)
- [How long a child lives](#how-long-a-child-lives)
- [Limits](#limits)
- [What changes](#what-changes)
- [Decisions](#decisions)
- [Later](#later)
- [Not supported](#not-supported)
- [Tests](#tests)
- [The work](#the-work)

## Goal

An agent hands work to another agent and **goes on**. A launched sub-agent ([runner](runner.md#launching-agents)) blocks its parent until it
returns. A **background agent** does not: the agent starts it, gets a job id at once, and carries on, and the result comes back when it is
ready. A chat agent can start a long piece of research and keep talking; a pipeline agent can start several and collect them.

## The principle

**A background agent is a job that is exposed to an agent as a tool.** It is not a new kind of thing: it has a job row, events, a
result in the user's store, and it can run in your process or on a worker, as any job does.

- **featherweight wakes nothing.** It emits events, and the application decides what to do with them. The model never polls. The
  one thing that waits for a child is a run that is not a chat ([how long a child lives](#how-long-a-child-lives)).
- **featherweight is stateless about conversations.** It keeps no pending result for a parent whose turn is over. A restart loses what
  the application has not stored, and the guide says so.
- **A session that wakes itself is not part of the framework.** It is an example, built on jobs and these events.
- **The result is read from the user's result store,** because we keep none. Background agents need a result store.

## The tool

An agent declares which agents it may start in the background, as it declares `subagents`:

```python
class Researcher(AgentBase):
    background_agents = [WebSearcher, Summariser]  # agent classes, or Job classes
```

- Every entry is an agent class (not a streaming one) or a `Job` class, as `submit` takes; none is the agent's own class; the names are
  distinct and different from its `subagents`. An agent that is itself in a `background_agents` list may not declare `background_agents`:
  **a background agent cannot start background agents** (a bound on fan-out).
- **The model gets one tool,** `start_background(agent_name, input_json)`, and the prompt lists the agents by name and description,
  as it does for `subagents`. `get_agent_schema(agent_name)` covers them too. `input_json` is a string, for the same reason as in
  `launch_agent`.
- **It returns at once, and as text,** and never raises into the parent's run:

| Case | What comes back |
|---|---|
| Started | `{"status": "STARTED", "agent": "...", "job_id": "..."}` |
| A name that is not on the list | `'x' is not an agent you can start in the background. You can start: ...` |
| The input does not fit | `The input for 'x' is not valid: ... Check get_agent_schema and try again.` |
| The limit of live children is reached | A sentence saying so |

- **The child is a job** with its own id. Its row records `parent_agent_run_id`, the attempt that started it (metadata only), so that the
  tree of a trace is one query. Its agent runs are those of an ordinary job: its own retries, tools and sub-agents. It does not share the
  parent's job and its run store.

## Where it runs

Where a child runs is the application's configuration, and nothing else changes with it: **the same job, here or on a worker**.

- **Here (the default).** The child runs in the parent's process, as an asyncio task, by the same code as `run_job`
  ([running a job class](jobs-and-tracing.md#running-a-job-class)). Nothing but the runner is needed.
- **On a worker.** `AgentRunner(db, background=client)`, where `client` is a `JobClient`: the child is submitted to the queue with
  `client.submit`, and a worker runs it. The workers must list the child's job in their `jobs`; a worker that does not know it fails the job,
  as it does for any job.

Either way the child's events and its result are those of any job, and an application that wants to follow it uses `client.events(job_id)`.
The runner needs a result store (`results=`) to read the result from; **an agent with `background_agents` run on a runner with
`results=None` is a `SetupError`** before any model call.

## Getting the result back

The spawn tool has returned and has its result ("started"). When the child finishes, its result cannot be a second result of that call: the
providers reject an orphan tool result, and a call with no result. So the result arrives as **new information**, as a user message:

```
<background_result job_id="…" agent="Summariser" status="SUCCESS">
{ the output, as JSON }
</background_result>
```

A failed child has `status="FAILED"` and its `error_type`, and a cancelled one `status="CANCELLED"`; there is no output.

- **While the parent's loop runs,** the runner adds the message of every child that has finished before the parent's next model call, **after**
  the results of the tool calls of the round (never between a call and its result), once for each child. This is a hook before the model call
  (LangChain's agent middleware; to be verified against the version in use when it is built).
- **After a chat turn has ended,** it is the application's: it starts the next turn with the history and the message.
  `await runner.background_message(job_id)` builds the same user message from the job's status and the result store, so that no
  application writes the format by hand (and it is how the example's session does it). It returns what it can: the job's metadata and, if the
  job succeeded, the result.

## Events

- **On the parent's stream** (a streaming agent, while the stream is open), two AG-UI `CUSTOM` events, as the streaming spec reserves
  ([streaming](streaming.md#the-events)): `background_started` (`job_id`, `agent`) and `background_finished` (`job_id`, `agent`, `status`,
  `error_type`). They are **metadata only**: the result is not in the event, the application reads it. They travel as any other event, over
  the adapters, and so over SSE.
- **After that stream has ended,** the application follows the child through its job events (`client.events(job_id)`, over SSE with
  `sse.encode`), as for any job. There is no new protocol and no new channel.
- **Observers:** the child's job span is under the tool call's span, and for a child on a worker it joins the trace through the carrier of
  the `submitted` hook, as any submitted job does ([observability](observability.md)).

## How long a child lives

- **A chat turn (a streaming agent) does not wait.** The turn ends when the model ends it, and its children carry on, in this process or on
  a worker. The application follows them with the events above. If the user **stops** the response (the cancel of a stream), the
  children of that run are cancelled with it.
- **A run that is not a chat waits.** When the model would end its run while it has live children, the run waits for the next one to finish, adds
  its result as above, and the model goes on, within `recursion_limit`; so a pipeline agent can start several children and collect them.
  Cancelling the run cancels its children.
- **Cancelling a child** is `cancel` as for any job: the task is cancelled (here), or `cancel_requested` is set (on a worker). A child that
  fails or is cancelled is a result like any other: the parent is told, and it is up to the model.
- **A parent that outlives its children's process** (a restart) loses the results that were not stored by the application. A child on a worker
  carries on and its result is in the store.

## Limits

- **Live children** (`max_background`, default 3): the most children of one attempt of one agent that may be running at one time. A start
  over the limit is refused with a sentence, and does not count. Finished children do not count.
- **No recursion:** a background agent cannot start background agents ([the tool](#the-tool)), and it is a job of its own, so `max_depth`
  and `max_launches` count within it, as in any job.
- The concurrency limit of the runner holds as for any run.

## What changes

Each in the spec that owns it: **agents** (the `background_agents` field and its checks); **the runner** (the tool, the limits, the hook before
the model call, `AgentRunner(background=)`, `runner.background_message`, cancel propagation); **persistence** (`jobs.parent_agent_run_id`);
**streaming** (the two `CUSTOM` events); **observability** (the child's span); **submitted jobs** (nothing: a child is a job).

## Decisions

- **A background agent is a job exposed as a tool,** and one concept runs here or on a worker, by configuration.
- **The result comes back as a user message,** never as a second tool result, and only where every call of the round has its result.
- **featherweight wakes nothing:** events, and a helper that builds the message. A self-waking session is an example.
- **A chat turn does not wait, and a run that is not a chat does.**
- **A background agent cannot start background agents,** and a child is not detached from its parent's cancel.
- **Metadata in the events,** and the result from the user's store.

## Later

- **Arbitrary statuses from a background agent** ("searching 3 of 5 sources"): a stream of updates, as progress, from a child to its parent
  and the application. It needs a place for them (a job event with content? a channel?), and the content question comes with it, so it is
  not decided here.
- **A child that outlives its parent** by design (detach) is a `client.submit`, and needs no new feature today.

## Not supported

- **Arbitrary statuses** and progress with content, for now ([later](#later)).
- **A durable inbox or a session** in the framework: pending results are the application's.
- **Background agents that start background agents.**
- **A tool for the model to wait, check or poll** a child.
- **Streaming agents as background agents** (they stream to a person, as for jobs).
- **A registry of running children,** or a way to list them: the jobs table has them (`parent_agent_run_id`).

## Tests

No live model call. Fake models, `MemoryQueue`, the dict result store.

- **The declaration:** the checks of `background_agents`, a background agent that declares its own refused, the prompt and the tool.
- **The tool:** every row of the table; a child here and on a worker (`MemoryQueue` and a worker); the same job, row, events and result in both;
  `results=None` refused before the model call; the limit of live children.
- **The result:** a child that finishes mid-loop is added after the round's tool results and once; a failed and a cancelled child; a model
  that starts several; the message of `runner.background_message`; no orphan tool result in any case.
- **The lifecycle:** a chat turn that ends with live children leaves them running; stopping the response cancels them; a run that is not a
  chat waits, goes on with the result, and cancels its children when it is cancelled.
- **The events and observers:** the two events on a stream and over the adapters; the child's span under the tool call's.

## The work

Tracked in [epic #115](https://github.com/aadhithya/featherweight/issues/115). One issue and one pull request each, in this order:

1. [#116](https://github.com/aadhithya/featherweight/issues/116) **The child job and the tool:** `background_agents`, `start_background`, `jobs.parent_agent_run_id`, here and on a worker, the limit.
2. [#117](https://github.com/aadhithya/featherweight/issues/117) **The result:** the hook before the model call, `runner.background_message`, and a run that waits for its children.
3. [#118](https://github.com/aadhithya/featherweight/issues/118) **Events and cancel:** the two `CUSTOM` events, stopping a response cancels its children, observers.
4. [#119](https://github.com/aadhithya/featherweight/issues/119) **The guide and the skill.**
5. [#120](https://github.com/aadhithya/featherweight/issues/120) **An example:** a session that wakes itself (a chat agent that starts a background agent and goes on).
