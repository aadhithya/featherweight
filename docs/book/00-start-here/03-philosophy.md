<!-- nav:top -->
[Book](../README.md) › [Part 0: Start here](../README.md#part-0-start-here) › The philosophy  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# The philosophy

featherweight has opinions. Take them, or fork off. Each one shaped features you will meet, and each is argued in a spec.

| Principle | What it means | Where you will see it |
|---|---|---|
| **Pydantic is the spec** | An agent's input and output are models, checked on the way in and on the way out. A bad declaration fails when the class is written. | [A typed agent](../01-your-first-agent/02-a-typed-agent.md), [agents spec](../../specs/agents.md) |
| **Failures are values** | A failed run returns a status and the error. It does not raise. | [Running it](../01-your-first-agent/03-running-it.md), [Failures and check_output](../01-your-first-agent/05-failures-and-check-output.md), [runner spec](../../specs/runner.md) |
| **Plain Python pipelines** | Order, loops and branching are `if` and `for`. No graph, no workflow syntax. | [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md), [jobs and tracing spec](../../specs/jobs-and-tracing.md) |
| **Least privilege** | Everything an agent can do is an allow-list that the developer wrote: its tools, the MCP servers and the tools it may use from them, its skills, the sub-agents it may launch, the agents it may start in the background. Nothing is granted by default. | [Tools](../02-abilities/01-tools.md), [MCP](../02-abilities/02-mcp.md), [Skills](../02-abilities/03-skills.md), [Sub-agents](../02-abilities/04-sub-agents.md), [Background agents](../05-in-production/03-background-agents.md), [tools and the store spec](../../specs/tools-and-store.md) |
| **Metadata only, and your storage** | The framework stores metadata and nothing else. Inputs, outputs and results go where you decide. | [Results and the result store](../03-jobs-and-pipelines/03-results-and-the-result-store.md), [persistence spec](../../specs/persistence.md) |
| **One concept, two places** | A job runs in your process or on a worker, by the same code, with the same record. | [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md), [Submit and a worker](../05-in-production/01-submit-and-a-worker.md), [submitted jobs spec](../../specs/submitted-jobs.md) |
| **We run no server** | We emit events, and SSE is the one streaming protocol. Your API sends them. | [Status and events](../03-jobs-and-pipelines/02-status-and-events.md), [In the browser](../04-chat/02-in-the-browser.md), [streaming spec](../../specs/streaming.md) |
| **What you plug in is an abstract class, with a kit** | A database, a result store, a queue or an observer is a class you implement, and a conformance kit checks it. | [Bring your own storage](../06-bring-your-own-storage/01-postgres.md), [persistence spec](../../specs/persistence.md) |
| **Seams, not solutions** | featherweight takes what you give it, as typed definitions, and runs it durably. How you store them, who your users are, who may do what, and what things are called is your application's. Where your code meets ours is a typed seam, checked at the boundary and closed when misused. | [Tenants](../../specs/persistence.md#tenants) (we have no tenant column: you choose the job id or the name), [Bring your own storage](../06-bring-your-own-storage/01-postgres.md) |

These are not a style guide for your code. They are why featherweight works the way it does, so that its behaviour is never a surprise.

<!-- nav:bottom -->
---

[← The map](02-the-map.md) · [Part 0: Start here](../README.md#part-0-start-here) · [How to read this book →](04-how-to-read.md)
<!-- /nav:bottom -->
