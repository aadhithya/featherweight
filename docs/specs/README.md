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
| [Observers and OpenTelemetry](observability.md) | The `Observer` class and what it receives, `OTelObserver` and the GenAI conventions, spans for jobs, attempts, model calls and tool calls, evaluation (designed, not built) |
| [Streaming agents](streaming.md) | `StreamingAgent`, `runner.stream()`, AG-UI events, thoughts, stopping a response, messages, the AG-UI and Vercel adapters |
| [Skills](skills.md) | The skill model, loading, how an agent reads a skill, the Agent Skills specification |
| [Tools, the registry and the store](tools-and-store.md) | Tools on an agent, the tool registry, the ready-made tools, the run context, the run store |
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
| [Observers and OpenTelemetry](observability.md) | The `Observer` class and its records, `OTelObserver`, the GenAI mapping, your own spans, evaluation |
| [Streaming agents](streaming.md) | `StreamingAgent`, `runner.stream()`, the events, the adapters |
| [Tools and the store](tools-and-store.md), [MCP tools](mcp-tools.md), [tool discovery](tool-discovery.md), [skills](skills.md) | What their names say |

A spec says what it owns, and links to the spec that owns the rest.
