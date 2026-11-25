# Documentation

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

Five kinds of page, from "how do I build it" to "why is it this way". Start at the top and go down only as far as you need.

| Level | Answers | Where |
|---|---|---|
| **Overview** | What is featherweight, and is it for me? | The [README](../README.md) |
| **The book** | How do I build something, step by step? | [`book/`](book/README.md) |
| **Recipes** | How do I put a few parts together for one job? | [`recipes/`](recipes/README.md) |
| **Guides** | How do I do X? | [This folder](#guides) |
| **Specs** | What exactly does it do, what was decided and why, and what is left out? | [`specs/`](specs) |
| **Examples** | Show me running code | [`examples/`](../examples) |

The book teaches in order; the guides and the specs are for looking things up. A word that means something particular is in [the glossary](book/07-reference/03-the-glossary.md), which also says which of two names to use. The guides and the specs cover the same features. A guide is for using one, and its spec is for relying on it, changing it,
or finding out why it behaves as it does.

## Where to start

- **I want to build something, step by step.** [The book](book/README.md): a short chapter at a time, one small project that grows from a
  typed agent to a chat and a pipeline on workers. Start with [the map](book/00-start-here/02-the-map.md). The guides and the specs below are the reference.
- **I want a short pattern to adapt.** [Recipes](recipes/README.md): a pipeline, resuming a job, a fake model for tests, a worker, a chat that
  streams, each one small and complete.
- **I'm building a platform where users define skills or agents.** [Recipes](recipes/README.md): a skill from a database or S3, agents built from JSON with your own rules, a queue that signs its messages,
  a tool that acts for the signed-in user. Then [who is asking](agents.md#who-is-asking-the-identity-of-a-run) and [jobs that are not declared in code](submitted-jobs.md#jobs-that-are-not-declared-in-code).
- **I want a coding agent to build with it.** [The skill](../skills/featherweight/SKILL.md): install it in Claude Code, or copy
  `skills/featherweight` into another tool's skills folder. It carries the rules, a working app, the book and the guides.
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
- **I want an agent to start another and go on.** [Background jobs](background-jobs.md): a job exposed to an agent as a tool, with the
  result coming back as a message. Then [`examples/background`](../examples/background).
- **I want to run jobs in the background.** [Submitted jobs](submitted-jobs.md): a client submits, a worker runs, and you follow
  it with events. Then [`examples/jobs`](../examples/jobs).
- **I want to run it in production.** [A production stack](production-stack.md): what to use where, with Postgres, your own queue and result store.
- **I want to see what my jobs and agents do.** [Observability](observability.md): OpenTelemetry spans for jobs, attempts, model calls
  and tool calls, ready for an evaluation platform. Then [`examples/observability`](../examples/observability).
- **I want to work on featherweight itself.** [Development](development.md): the commands, the layout, how to contribute.
- **I want to know why it works this way, or to change it.** [Specs](specs). Start with the
  [agent runner](specs/runner.md), which most behaviour passes through.

## Guides

| Guide | Covers | Its spec |
|---|---|---|
| [Getting started](getting-started.md) | Installing it, a first agent, running it, the examples | |
| [Agents](agents.md) | Defining and running an agent, results, retries, output checks, tools, who is asking, configuration | [Agents](specs/agents.md), [the runner](specs/runner.md), [tools and the store](specs/tools-and-store.md) |
| [Models and providers](models.md) | `Gemini`, `Gemma`, `Claude`, `OpenAI`, what is checked, adding a model or a provider | [Models](specs/models.md) |
| [Launching agents](launching-agents.md) | The `subagents` allow-list, the two tools, depth and breadth, concurrency, tracing | [The runner](specs/runner.md#launching-agents) |
| [MCP tools](mcp.md) | Declaring servers, connections, what the model gets, failures, finding tools by search | [MCP tools](specs/mcp-tools.md), [tool discovery](specs/tool-discovery.md) |
| [Skills](skills.md) | The skill model, how an agent reads its skills, loading, what the specification covers | [Skills](specs/skills.md) |
| [Streaming agents](streaming.md) | `StreamingAgent`, `runner.stream()`, the events, thoughts, the AG-UI and Vercel adapters, stopping a response, keeping the conversation, the example | [Streaming agents](specs/streaming.md) |
| [What the providers accept as attachments](attachments-providers.md) | A dated snapshot of what Anthropic, Google, OpenAI and Ollama accept: types, how, sizes, counts, with links | [Attachments](specs/attachments.md) |
| [Persistence](persistence.md) | What the run log holds and what it never does, result stores, choosing a database, migrations, tenants | [Persistence](specs/persistence.md) |
| [Jobs](jobs.md) | Declaring a job as a class or with `@job`, `run_job`, following it with events and SSE, resuming a failed job | [Jobs](specs/jobs.md) |
| [Submitted jobs](submitted-jobs.md) | `JobClient`, `JobWorker` and `JobQueue`: submitting a job, cancelling, several workers and takeover, your own queue, the example | [Submitted jobs](specs/submitted-jobs.md) |
| [Background jobs](background-jobs.md) | `background_jobs`, `start_background`, here or on a worker, the result message, the events, a chat in the browser, how long a child lives | [Background jobs](specs/background-jobs.md) |
| [Observability](observability.md) | Observers, `OTelObserver`, what is on the spans, content, your own spans, evaluation, the convention versions | [Observers and OpenTelemetry](specs/observability.md) |
| [A production stack](production-stack.md) | What to use where: Postgres, your queue, your result store, the API, workers, traces; the wiring | [Persistence](specs/persistence.md), [submitted jobs](specs/submitted-jobs.md) |
| [Example pipeline](example-pipeline.md) | `doclearn`: what it does, its agents, how it handles failures, and its limits | |
| [Migrating from 0.1](migration.md) | What breaks between 0.1 and the next release, what to search for, what to write instead, and the same for a coding agent | [The units of work](specs/units.md) |
| [Development](development.md) | The make commands, the layout of the repository, contributing | |

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
  │   └── jobs ────────── a pipeline: its status, events and resuming
  │
  ├── tools ───────────── the tools of an attempt, the registry
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
