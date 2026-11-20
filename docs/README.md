# Documentation

Three kinds of page, from "how do I use it" to "why is it this way". Start at the top and go down only as far as you need.

| Level | Answers | Where |
|---|---|---|
| **Overview** | What is featherweight, and is it for me? | The [README](../README.md) |
| **Guides** | How do I do X? | [This folder](#guides) |
| **Specs** | What exactly does it do, what was decided and why, and what is left out? | [`specs/`](specs) |
| **Examples** | Show me running code | [`examples/`](../examples) |

The guides and the specs cover the same features. A guide is for using one, and its spec is for relying on it, changing it,
or finding out why it behaves as it does.

## Where to start

- **I want a coding agent to build with it.** [The skill](../skills/featherweight/SKILL.md): install it in Claude Code, or copy
  `skills/featherweight` into another tool's skills folder. It carries the rules, a working app and the guides.
- **I'm new here.** [Getting started](getting-started.md): install it, write an agent, run it.
- **I want to build an agent.** [Agents](agents.md): define one, run it, read the result, handle failures. Then
  [`examples/launching`](../examples/launching) or [`examples/skills`](../examples/skills) to run something.
- **I want my agent to use outside tools.** [MCP tools](mcp.md): declare a server, and the agent gets its tools. They are
  found by search by default. Then [`examples/mcp`](../examples/mcp), which uses GitHub's server.
- **I want to use another model or provider.** [Models and providers](models.md): Gemini, Gemma, Claude and OpenAI, and how
  to add GPT-5.x or Ollama with a small class.
- **I want agents that delegate.** [Launching agents](launching-agents.md): an allow-list of agents, depth and breadth
  limits.
- **I want to teach an agent a procedure.** [Skills](skills.md): instructions in the Agent Skills format, read on demand.
- **I want to see a whole pipeline.** [Example pipeline](example-pipeline.md): `doclearn`, five agents and two pipelines
  built on featherweight. Run it with `make pipeline`.
- **I want an agent to start another and go on.** [Background agents](background-agents.md): a job exposed to an agent as a tool, with the
  result coming back as a message. Then [`examples/background`](../examples/background).
- **I want to run jobs in the background.** [Submitted jobs](submitted-jobs.md): a client submits, a worker runs, and you follow
  it with events. Then [`examples/jobs`](../examples/jobs).
- **I want to see what my jobs and agents do.** [Observability](observability.md): OpenTelemetry spans for jobs, attempts, model calls
  and tool calls, ready for an evaluation platform. Then [`examples/observability`](../examples/observability).
- **I want to work on featherweight itself.** [Development](development.md): the commands, the layout, how to contribute.
- **I want to know why it works this way, or to change it.** [Specs](specs). Start with the
  [agent runner](specs/runner.md), which most behaviour passes through.

## Guides

| Guide | Covers | Its spec |
|---|---|---|
| [Agents](agents.md) | Defining and running an agent, results, retries, output checks, tools, jobs and tracing, configuration | [Agents](specs/agents.md), [the runner](specs/runner.md), [jobs and tracing](specs/jobs-and-tracing.md), [tools and the store](specs/tools-and-store.md) |
| [Models and providers](models.md) | `Gemini`, `Gemma`, `Claude`, `OpenAI`, what is checked, adding a model or a provider | [Models](specs/models.md) |
| [Launching agents](launching-agents.md) | The `subagents` allow-list, the two tools, depth and breadth, concurrency, tracing | [The runner](specs/runner.md#launching-agents) |
| [MCP tools](mcp.md) | Declaring servers, connections, what the model gets, failures, finding tools by search | [MCP tools](specs/mcp-tools.md), [tool discovery](specs/tool-discovery.md) |
| [Background agents](background-agents.md) | `background_agents`, `start_background`, here or on a worker, the result message, the events, a chat in the browser, how long a child lives | [Background agents](specs/background-agents.md) |
| [Streaming agents](streaming.md) | `StreamingAgent`, `runner.stream()`, the events, thoughts, the AG-UI and Vercel adapters, stopping a response, keeping the conversation, the example | [Streaming agents](specs/streaming.md) |
| [Getting started](getting-started.md) | Installing it, a first agent, running it, the examples | |
| [Development](development.md) | The make commands, the layout of the repository, contributing | |
| [Persistence](persistence.md) | What is kept and what is not, resuming a job, result stores, choosing a database, tenants | [Persistence](specs/persistence.md) |
| [Submitted jobs](submitted-jobs.md) | A job as a class or a decorator, `JobClient`, `JobWorker`, `JobQueue`, cancelling, job events and SSE, the example | [Submitted jobs](specs/submitted-jobs.md) |
| [Observability](observability.md) | Observers, `OTelObserver`, what is on the spans, content, your own spans, evaluation, the convention versions | [Observers and OpenTelemetry](specs/observability.md) |
| [Skills](skills.md) | The skill model, how an agent reads its skills, loading, what the specification covers | [Skills](specs/skills.md) |
| [Example pipeline](example-pipeline.md) | `doclearn`: what it does, its agents, how it handles failures, and its limits | |

## Specs

Each spec has the same shape: goal, the rules, decisions and why, what is not supported, and which tests cover it. The
[index](specs/README.md) lists them. How they fit together:

```
agents ──────────── what an agent is, and what is checked when it is built
  │
runner ──────────── runs agents: attempts, retries, concurrency, launching other agents
  │   │
  │   ├── models ──────── which model and provider, and how hard it thinks
  │   │
  │   └── jobs and tracing ── what is recorded, and how
  │
  ├── tools and store ──── the tools of an attempt, the registry, the run store
  │       │
  │       ├── MCP tools ───────── servers, connections, failures
  │       │       └── tool discovery ── finding MCP tools by search
  │
  └── skills ───────────── instructions an agent reads when it needs them
```

## How it fits together

- **The README** is the landing page: what featherweight is, what it does, and how it differs.
- **A guide** is written for someone with a task. It shows code, and says what to expect.
- **A spec** is written from the code. It states the rules as they are, the decisions with their reasons, and what was left
  out on purpose. If a guide and a spec disagree, the spec and the code are right, and the guide should be fixed.
- **An example** is a folder you can run (`make example-skills`, `make example-launching`, `make example-mcp`, `make
  example-resume`, `make example-jobs`, `make example-observability`, `make pipeline`). Each has a README that says what to look at.
- **Releasing** is for the maintainers: [Releasing](releasing.md) says how a release is made, and the changelog is
  [`CHANGELOG.md`](../CHANGELOG.md).
- **The tests** are in [`tests/`](../tests), and each spec says which files cover it. `tests/e2e` uses the real model.
