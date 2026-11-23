# Specs

What each part of featherweight is supposed to do, and why. ([Documentation home](../README.md) shows how these fit with the guides.) The pages in [`docs`](..) say how to use it; these say what was
decided, what was checked, and what is deliberately left out. Each follows the same shape: goal, the rules, decisions,
not supported, tests.

| Spec | Covers |
|---|---|
| [Agents](agents.md) | The agent definition, what is checked when it is built, input and output, the system prompt |
| [The agent runner](runner.md) | Running, results, attempts and retries, concurrency, **launching agents**, limits |
| [Models and providers](models.md) | `ModelSettings`: Gemini, Gemma, Claude and OpenAI, the catalog, temperature, extending it, tokens |
| [Jobs and tracing](jobs-and-tracing.md) | Jobs, the run log, statuses, logging |
| [The run log and the persistence boundary](persistence.md) | What featherweight keeps and never keeps, hashes, `Database` and `ResultStore`, migrations, the schema, resumable jobs, Postgres |
| [Submitted jobs](submitted-jobs.md) | A job as a declared class, `JobQueue`, `JobClient`, `JobWorker`, cancelling, job events and SSE |
| [The units of work](units.md) | The words job, execution, run, attempt and step, the ids and names that follow from them, and the rename from the old words |
| [Identity](identity.md) | Who a run is for: a typed identity the application passes, how tools read it, how it crosses the queue, and the identity hash for resuming; what stays the application's |
| [Observers and OpenTelemetry](observability.md) | The `Observer` class and what it receives, `OTelObserver` and the GenAI conventions, spans for jobs, attempts, model calls and tool calls, evaluation |
| [Background jobs](background-jobs.md) | A job exposed to an agent as a tool, here or on a worker, getting its result back, events, how long a child lives |
| [Streaming agents](streaming.md) | `StreamingAgent`, `runner.stream()`, AG-UI events, thoughts, stopping a response, messages, the AG-UI and Vercel adapters |
| [Attachments](attachments.md) | Images, documents, audio and video in a chat message: what a client can send, what the model gets, limits, failures |
| [Skills](skills.md) | The skill model, loading, how an agent reads a skill, the Agent Skills specification |
| [Tools and the registry](tools-and-store.md) | Tools on an agent, the tool registry, the ready-made tools, the run context |
| [Tool discovery](tool-discovery.md) | Finding MCP tools by keyword: `discover`, `search_tools`, `call_tool`, matching |
| [MCP tools](mcp-tools.md) | MCP servers: declaring, connections, failures, what the model gets |

## Where things belong

A change goes in the spec of the thing it changes, and not in the spec of the epic that happens to touch it. Each spec owns:

| Spec | Owns |
|---|---|
| [Agents](agents.md) | The agent definition, what is checked when it is built, input and output, the system prompt |
| [The agent runner](runner.md) | Running an agent: results, attempts and retries, concurrency, launching agents, how a step is resumed, when observers are called |
| [Models and providers](models.md) | `ModelSettings`, the catalog, what is checked per provider, tokens |
| [Jobs and tracing](jobs-and-tracing.md) | A job: `run_job`, running a job class, its version, statuses, what the run log records, logging |
| [Submitted jobs](submitted-jobs.md) | The `Job` class and `@job`, the queue and its message, the client, the worker, cancelling, job events and SSE |
| [Persistence](persistence.md) | The metadata-only rule, hashes, the `Database` class, the schema of every table, migrations, the result store, Postgres, tenants |
| [The units of work](units.md) | Job, execution, run, attempt and step: what each is, its id, where it appears (the API, the database, the spans, the logs, the settings) |
| [Identity](identity.md) | The identity of a run: declaring and passing it, `RunContext.identity`, who inherits it, its field in the queue message, `identity_hash`, the developer's responsibilities |
| [Observers and OpenTelemetry](observability.md) | The `Observer` class and its records, `OTelObserver`, the GenAI mapping, your own spans, evaluation |
| [Background jobs](background-jobs.md) | `background_jobs`, `start_background`, where a child runs, the result message, the events, how long a child lives |
| [Streaming agents](streaming.md) | `StreamingAgent`, `runner.stream()`, the events, the adapters |
| [Attachments](attachments.md) | The parts a user message may carry, how they reach the model, the limits on them |
| [Tools and the registry](tools-and-store.md), [MCP tools](mcp-tools.md), [tool discovery](tool-discovery.md), [skills](skills.md) | What their names say |

A spec says what it owns, and links to the spec that owns the rest.
