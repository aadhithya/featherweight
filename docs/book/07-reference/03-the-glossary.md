<!-- nav:top -->
[Book](../README.md) › [Part 7: Reference](../README.md#part-7-reference) › The glossary
<!-- /nav:top -->

# The glossary

Look up a word.

| Term | Meaning |
|---|---|
| **agent** | A typed class: an input, an output, a prompt and what it may use. [A typed agent](../01-your-first-agent/02-a-typed-agent.md) |
| **attempt** | One try of one agent call. A failed attempt is retried, up to three. Not an execution. [Failures](../01-your-first-agent/05-failures-and-check-output.md) |
| **background agent** | An agent that another agent starts and does not wait for. It is a job. [Background agents](../05-in-production/03-background-agents.md) |
| **check_output** | A method of an agent that rejects an output that a schema cannot, by raising. [Failures](../01-your-first-agent/05-failures-and-check-output.md) |
| **content** | Inputs, outputs, messages, tool arguments and results, and error text. Never stored: it goes only to an observer that asks. [An observer](../06-bring-your-own-storage/05-an-observer.md) |
| **conformance kit** | `testing.check_database`, `check_result_store`, `check_queue`, `check_observer`: they run the contract against your own implementation. [A result store](../06-bring-your-own-storage/02-a-result-store.md) |
| **database** | Where the run log is kept: `SqliteDatabase`, `PostgresDatabase` or yours. [Postgres](../06-bring-your-own-storage/01-postgres.md) |
| **delivery count** | How many times a queue has delivered a message. Higher than one means it came back. [A job queue](../06-bring-your-own-storage/03-a-job-queue.md) |
| **event** | One record for each change of a job's status (`queued`, `running`, `succeeded`...), streamed as SSE. [Status and events](../03-jobs-and-pipelines/02-status-and-events.md) |
| **execution** | How many times a worker has started a job: one, and one more for each takeover. Not an attempt. [Several workers](../05-in-production/02-several-workers.md) |
| **extra** | An optional install: `featherweight[mcp]`, `[anthropic]`, `[openai]`, `[postgres]`, `[otel]`, or `[all]`. [Install](../01-your-first-agent/01-install-and-set-up.md) |
| **hash** | A fingerprint of an agent, a prompt or an input. It is stored instead of the content. [What happened](../01-your-first-agent/04-what-happened.md) |
| **history** | The messages of a chat. It is yours: featherweight keeps none. [Stopping and the conversation](../04-chat/03-stopping-and-the-conversation.md) |
| **job** | A declared class, or a function with `@job`, that runs in your process or on a worker. [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md) |
| **lease** | How long a worker holds a message and a job. It renews it while it lives; if it dies, the lease runs out. [Several workers](../05-in-production/02-several-workers.md) |
| **MCP** | Model Context Protocol: a server that gives an agent tools. Stdio servers run as a subprocess, HTTP servers run elsewhere. [MCP](../02-abilities/02-mcp.md) |
| **metadata** | Status, tokens, timing, hashes and error types: all that the framework stores. [What happened](../01-your-first-agent/04-what-happened.md) |
| **model settings** | The model of an agent, as a setting: Gemini, Gemma, Claude, OpenAI or your own. [Choosing a model](../01-your-first-agent/06-choosing-a-model.md) |
| **observer** | A class the runner calls as things happen; `OTelObserver` is the one we ship. [An observer](../06-bring-your-own-storage/05-an-observer.md) |
| **pipeline** | Plain Python that runs agents: order, loops and branching are `if` and `for`. [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md) |
| **queue** | How a submitted job travels to a worker: `SqliteQueue`, `MemoryQueue` or yours. [Submit and a worker](../05-in-production/01-submit-and-a-worker.md) |
| **resume, resumable** | A job made `resumable` keeps its step results, so a later job can run again without redoing them. [Resume](../03-jobs-and-pipelines/04-resume.md) |
| **result store** | Where job results are kept: `SqliteResultStore` or yours. [Results and the result store](../03-jobs-and-pipelines/03-results-and-the-result-store.md) |
| **run log** | The rows of every job and every attempt, metadata only. [What happened](../01-your-first-agent/04-what-happened.md) |
| **runner** | `AgentRunner`: it runs agents with retries, limits and a log. [Running it](../01-your-first-agent/03-running-it.md) |
| **skill** | Instructions an agent reads when it needs them. [Skills](../02-abilities/03-skills.md) |
| **span** | A unit of an OpenTelemetry trace: a job, an attempt, a model call or a tool call. [Observability](../05-in-production/04-observability.md) |
| **SSE** | Server-sent events: the only streaming protocol featherweight uses, for chat and job events. [In the browser](../04-chat/02-in-the-browser.md) |
| **streaming agent** | An agent that streams its answer, for chat. [A streaming agent](../04-chat/01-a-streaming-agent.md) |
| **sub-agent** | An agent that another agent launches and waits for. [Sub-agents](../02-abilities/04-sub-agents.md) |
| **takeover** | Another worker taking a job whose worker died, as the same job with a higher execution. [Several workers](../05-in-production/02-several-workers.md) |
| **tool** | A function an agent may call. [Tools](../02-abilities/01-tools.md) |
| **worker** | `JobWorker`: the process that takes jobs from a queue and runs them. [Submit and a worker](../05-in-production/01-submit-and-a-worker.md) |

<!-- nav:bottom -->
---

[← The specs](02-the-specs.md) · [Part 7: Reference](../README.md#part-7-reference) · [The finished project →](04-the-finished-project.md)
<!-- /nav:bottom -->
