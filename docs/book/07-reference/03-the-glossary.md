<!-- nav:top -->
[Book](../README.md) › [Part 7: Reference](../README.md#part-7-reference) › The glossary  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# The glossary

Look up a word.

These are the words the docs use, and what they mean. A word has one name: the left column is the one to use, in the docs and in your own code and comments. [Words we use, and words we
do not](#words-we-use-and-words-we-do-not) lists the names that look the same and are not used.

| Term | Meaning |
|---|---|
| **agent** | A typed class: an input, an output, a prompt and what it may use. A running agent is complete and validated before it runs, and its hash identifies it. [A typed agent](../01-your-first-agent/02-a-typed-agent.md) |
| **approval** | A person's yes to a proposal. The client sends the proposal's id, the choice and the hash it was shown; your own code checks them and acts, once. [Approvals](../05-in-production/06-approvals.md) |
| **allow-list** | A list the developer writes of what an agent may use: its tools, MCP servers, skills, sub-agents and background jobs. Nothing is granted by default. [Philosophy](../00-start-here/03-philosophy.md) |
| **attachment** | A file (an image, a document, audio or video) sent with a chat message, as inline bytes. featherweight passes it on and checks nothing but the limits. [Attaching a file](../04-chat/04-attaching-a-file.md) |
| **attempt** | One try within a run. A failed attempt is retried, up to three times. Each attempt is a row of the run log. Not a run, and not an execution. [Failures](../01-your-first-agent/05-failures-and-check-output.md) |
| **background job** | A job that an agent starts with `start_background` and does not wait for. The agent lists the agents and jobs it may start in `background_jobs`, and an agent class in that list runs as a job of its own. [Background jobs](../05-in-production/03-background-jobs.md) |
| **catalog** | The list your application keeps of the tools, models or skills that users may pick from when they define an agent. featherweight has none: the builder is yours. [Agents built from JSON](../../recipes/agents-built-from-json.md) |
| **check_output** | A method of an agent that rejects an output that a schema cannot, by raising. [Failures](../01-your-first-agent/05-failures-and-check-output.md) |
| **content** | Inputs, outputs, messages, tool arguments and results, and error text. Never stored: it goes only to an observer that asks. [An observer](../06-bring-your-own-storage/05-an-observer.md) |
| **conformance kit** | `testing.check_database`, `check_result_store`, `check_queue`, `check_observer`: they run the contract against your own implementation. [A result store](../06-bring-your-own-storage/02-a-result-store.md) |
| **database** | Where the run log is kept: `SqliteDatabase`, `PostgresDatabase` or yours. [Postgres](../06-bring-your-own-storage/01-postgres.md) |
| **delivery count** | How many times a queue has delivered a message. Higher than one means it came back. [A job queue](../06-bring-your-own-storage/03-a-job-queue.md) |
| **depth** | How far down a chain of launches an agent is: 1 at the top. `max_depth` limits it. [Sub-agents](../02-abilities/04-sub-agents.md) |
| **event** | One record for each change of a job's status (`queued`, `running`, `succeeded`...), streamed as SSE. [Status and events](../03-jobs-and-pipelines/02-status-and-events.md) |
| **execution** | How many times a worker has started a job: one, and one more for each takeover. Not an attempt. [Several workers](../05-in-production/02-several-workers.md) |
| **extra** | An optional install: `featherweight[mcp]`, `[anthropic]`, `[openai]`, `[postgres]`, `[otel]`, or `[all]`. [Install](../01-your-first-agent/01-install-and-set-up.md) |
| **hash** | A fingerprint, stored instead of the content: `agent_hash` (the whole declaration), `job_hash`, `input_hash`, `output_hash` and `identity_hash`. [What happened](../01-your-first-agent/04-what-happened.md) |
| **history** | The messages of a chat. It is yours: featherweight keeps none. [Stopping and the conversation](../04-chat/03-stopping-and-the-conversation.md) |
| **identity** | Who a run is for: a small Pydantic model that your application defines and passes as `identity=`. Tools read it from their `RunContext`, a launched agent inherits it, and it crosses the queue to a worker. A job is for one identity. It is never shown to the model, stored or logged, and it is not a credential. Declared with `identity_type`. [Who is asking](../05-in-production/05-who-is-asking.md) |
| **job** | A declared class, or a function with `@job`, that runs in your process or on a worker. It has a name, an id, a status, events and a result. [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md) |
| **job name** | The string a worker runs a job by. It is yours to choose, unique in what the worker looks in, stable, and it matches `[A-Za-z][A-Za-z0-9_.-]*`. [Identity spec](../../specs/identity.md#the-developers-responsibilities) |
| **jobs_lookup** | The worker's fallback for jobs that are not declared in code: it is called only for a name that `jobs` does not have, and what it returns is checked. [Jobs that are not declared in code](../../submitted-jobs.md#jobs-that-are-not-declared-in-code) |
| **launch** | An agent starts a sub-agent with `launch_agent` and waits for its result. [Sub-agents](../02-abilities/04-sub-agents.md) |
| **lease** | How long a worker holds a message and a job. It renews it while it lives; if it dies, the lease runs out. [Several workers](../05-in-production/02-several-workers.md) |
| **MCP** | Model Context Protocol: a server that gives an agent tools. Stdio servers run as a subprocess, HTTP servers run elsewhere. [MCP](../02-abilities/02-mcp.md) |
| **message** | What goes on a queue for a submitted job: the job id, the job name, the input, the identity and the trace context. [A job queue](../06-bring-your-own-storage/03-a-job-queue.md) |
| **metadata** | Status, tokens, timing, hashes and error types: all that the framework stores. [What happened](../01-your-first-agent/04-what-happened.md) |
| **model settings** | The model of an agent, as a setting: Gemini, Gemma, Claude, OpenAI or your own. [Choosing a model](../01-your-first-agent/06-choosing-a-model.md) |
| **observer** | A class the runner calls as things happen; `OTelObserver` is the one we ship. [An observer](../06-bring-your-own-storage/05-an-observer.md) |
| **pipeline** | Plain Python that runs agents: order, loops and branching are `if` and `for`. When it is declared as a class or with `@job`, it is a job. [What a job is](../03-jobs-and-pipelines/01-what-a-job-is.md) |
| **queue** | How a submitted job travels to a worker: `SqliteQueue`, `MemoryQueue` or yours. [Submit and a worker](../05-in-production/01-submit-and-a-worker.md) |
| **proposal** | What an agent returns instead of acting: a typed value (its output, or what a `propose_*` tool writes) that your application keeps with its hash and an expiry, shows to a person, and executes after the approval. [Approvals](../05-in-production/06-approvals.md) |
| **recipe** | A short, complete pattern for one job, in `docs/recipes`. Its code is run by the tests. [Recipes](../../recipes/README.md) |
| **reference** | An id or a name for a file or a record that you keep in a store of your own. The input of a job or an agent carries it, and a tool fetches what it points to and checks who may read it: files do not travel between agents or through a queue. [Files for an agent or a job](../../recipes/files-for-an-agent-or-a-job.md) |
| **run** | One call of an agent, with its retries: what `runner.run(agent, input)` is and what returns an `AgentResult`. One streamed turn is a run. A run makes one or more attempts. Used as a verb, "to run" starts anything; as a noun, "a run" is only this. [Running it](../01-your-first-agent/03-running-it.md) |
| **resume, resumable** | A job made `resumable` keeps its step results, so a later job (made with `resume_from`) can run again without redoing them. Only a step of the same agent, input and identity is reused. [Resume](../03-jobs-and-pipelines/04-resume.md) |
| **result store** | Where job results are kept: `SqliteResultStore` or yours. [Results and the result store](../03-jobs-and-pipelines/03-results-and-the-result-store.md) |
| **run log** | The rows of every job and every attempt, metadata only. [What happened](../01-your-first-agent/04-what-happened.md) |
| **runner** | `AgentRunner`: it runs agents with retries, limits and a log. [Running it](../01-your-first-agent/03-running-it.md) |
| **seam** | Where your code meets featherweight: a typed argument, an abstract class or a hook, checked at the boundary and closed when misused. [Philosophy](../00-start-here/03-philosophy.md) |
| **skill** | Instructions an agent reads when it needs them, in the Agent Skills format. [Skills](../02-abilities/03-skills.md) |
| **span** | A unit of an OpenTelemetry trace: a job, an attempt, a model call or a tool call. [Observability](../05-in-production/04-observability.md) |
| **SSE** | Server-sent events: the only streaming protocol featherweight uses, for chat and job events. [In the browser](../04-chat/02-in-the-browser.md) |
| **step** | A run made directly by the job's own code (depth 1): what a resume reuses. A role of a run, not a unit of its own. [Resume](../03-jobs-and-pipelines/04-resume.md) |
| **streaming agent** | An agent that streams its answer, for chat: a `StreamingAgent`. [A streaming agent](../04-chat/01-a-streaming-agent.md) |
| **sub-agent** | An agent that another agent launches and waits for. Written `sub-agent` in prose, and `subagents` for the field that lists them. [Sub-agents](../02-abilities/04-sub-agents.md) |
| **takeover** | Another worker taking a job whose worker died, as the same job with a higher execution. [Several workers](../05-in-production/02-several-workers.md) |
| **tool** | A function an agent may call. [Tools](../02-abilities/01-tools.md) |
| **trace context** | The W3C carrier of a trace (`traceparent`) that travels in a queue message, so that a worker's span joins the trace of the client. [Observability](../05-in-production/04-observability.md) |
| **worker** | `JobWorker`: the process that takes jobs from a queue and runs them. [Submit and a worker](../05-in-production/01-submit-and-a-worker.md) |

## Words we use, and words we do not

Where two names could mean one thing, the docs use one.

| Use | Not | Why |
|---|---|---|
| **sub-agent**, and `subagents` for the field | subagent, child agent, delegate | One spelling in prose. `launch_agent` is the tool, "launch" the verb. |
| **background job**, and `background_jobs` for the field | background agent, background child | What it holds is a job, and an agent class in it runs as one. The tool is still `start_background`. |
| **worker** | job worker | `JobWorker` is the class; the process is a worker. |
| **run log** | run store, agent store | The run store was removed. The log holds metadata only. |
| **result store** | results store | `ResultStore` is the class. |
| **streaming agent** | chat agent | `StreamingAgent` is the class, and it is used for chat. |
| **allow-list** | allowlist, whitelist | One spelling. |
| **identity** | caller, principal, actor, user (for what is passed) | "Caller" is the model calling a tool, and "actor" is the other side of a delegation. |
| **run** / **attempt** / **execution** | "invocation" or "agent run" for a run, "run" for an attempt or a job | A run is one call of an agent, an attempt is one try within it, an execution is one start of a job by a worker. A job is never "a run of a pipeline". |
| **resume** (a job), **reuse** (a step) | replay, rerun, for the same thing | A resume is a new job that points to an old one; it reuses the steps that finished. |
| **pipeline** (your code), **job** (a declared pipeline) | workflow | A job has a name, an id, a status and events. `workflow` is the OpenTelemetry word for it in a span name. |
| **featherweight**, lowercase | Featherweight | The name is written in lowercase, always. |

<!-- nav:bottom -->
---

[← The specs](02-the-specs.md) · [Part 7: Reference](../README.md#part-7-reference) · [The finished project →](04-the-finished-project.md)
<!-- /nav:bottom -->
