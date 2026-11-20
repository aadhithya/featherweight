<!-- nav:top -->
[Book](../README.md) › [Part 0: Start here](../README.md#part-0-start-here) › The philosophy
<!-- /nav:top -->

# The philosophy

featherweight has opinions. Take them, or fork off. Each one shaped features you will meet, and each is argued in a spec.

| Principle | What it means | Where you will see it |
|---|---|---|
| **Pydantic is the spec** | An agent's input and output are models, checked on the way in and on the way out. A bad declaration fails when the class is written. | [A typed agent](../01-your-first-agent/02-a-typed-agent.md), [agents spec](../../specs/agents.md) |
| **Failures are values** | A failed run returns a status and the error. It does not raise. | [Running it](../01-your-first-agent/03-running-it.md), [Failures and check_output](../01-your-first-agent/05-failures-and-check-output.md), [runner spec](../../specs/runner.md) |
| **Plain Python pipelines** | Order, loops and branching are `if` and `for`. No graph, no workflow syntax. | [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md), [jobs and tracing spec](../../specs/jobs-and-tracing.md) |
| **Metadata only, and your storage** | The framework stores metadata and nothing else. Inputs, outputs and results go where you decide. | [Results and the result store](../03-jobs-and-pipelines/03-results-and-the-result-store.md), [persistence spec](../../specs/persistence.md) |
| **One concept, two places** | A job runs in your process or on a worker, by the same code, with the same record. | [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md), [Submit and a worker](../05-in-production/01-submit-and-a-worker.md), [submitted jobs spec](../../specs/submitted-jobs.md) |
| **We run no server** | We emit events, and SSE is the one streaming protocol. Your API sends them. | [Status and events](../03-jobs-and-pipelines/02-status-and-events.md), [In the browser](../04-chat/02-in-the-browser.md), [streaming spec](../../specs/streaming.md) |
| **What you plug in is an abstract class, with a kit** | A database, a result store, a queue or an observer is a class you implement, and a conformance kit checks it. | [Bring your own storage](../06-bring-your-own-storage/01-postgres.md), [persistence spec](../../specs/persistence.md) |

These are not a style guide for your code. They are why featherweight works the way it does, so that its behaviour is never a surprise.

<!-- nav:bottom -->
---

[← The map](02-the-map.md) · [Part 0: Start here](../README.md#part-0-start-here) · [How to read this book →](04-how-to-read.md)
<!-- /nav:bottom -->
