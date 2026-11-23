# Spec: observers and OpenTelemetry

Status: implemented. #92 (the `Observer` class, its records, `testing.check_observer`, and the runner's calls), the job scope and the trace context across the queue (#93) and `OTelObserver` (#94, pinned to `semantic-conventions-genai@622b2b6`, which refers to core semantic conventions 1.45.0) and the guide, the example and the skill reference (#95) are done. It is tracked in [epic #90](https://github.com/aadhithya/featherweight/issues/90), and each part has its own issue. When
a part is built, this page says so, and the guide `docs/observability.md` shows how to use it.

It builds on [the persistence spec](persistence.md) (the rule that featherweight keeps metadata only, and that content never
goes into the run log, even by option). It is about what an observer is, what it receives, and the OpenTelemetry observer. What the
other specs do about it is in them, and not here: [the runner](runner.md#observers) (when it calls observers, and how it sees model calls
and tool calls), [submitted jobs](submitted-jobs.md#the-queue) (the trace context in the message), and [jobs and
tracing](jobs-and-tracing.md#running-a-job-class) (a job runs the same way in your process and on a worker, and has a version), which the
spans of a job depend on.

- [Goal](#goal)
- [The principle](#the-principle)
- [Observers](#observers)
- [What an observer sees](#what-an-observer-sees)
- [OpenTelemetry](#opentelemetry)
- [Your own spans](#your-own-spans)
- [Evaluation](#evaluation)
- [What changes](#what-changes)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)
- [The work](#the-work)

## Goal

Let a developer see what their pipelines do, and run evaluations on what they collected, without featherweight keeping or
seeing their data.

- **Observers:** an abstract class that the runner calls as things happen. featherweight stores nothing from it. What an
  observer does with the events is the user's.
- **OpenTelemetry:** one observer that we ship, because every observability and evaluation platform reads it. It follows the
  OpenTelemetry GenAI semantic conventions, so the traces are readable by those platforms as they are.

## The principle

**featherweight emits spans for its own concepts. The user uses the OpenTelemetry API for their own.**

- Ours: a job, an agent attempt, a model call, a tool call, and a reused step. Nothing else.
- Yours: any span around your own code. There is no featherweight API for it, no helper, no wrapper.
- **Metadata always, content on request.** The records an observer receives always carry ids, names, status, tokens, timing,
  hashes and error types. Inputs, outputs, messages, tool arguments and results and error text are there only for an observer
  that asks for them. Nothing leaves the process by default.
- **The run log is the source of truth.** Resume and status never read from an observer, and nothing an observer receives is
  stored by us. In particular **no trace id or span id is stored** in any table: they are application and telemetry state.
- **Observers never change a run.** One that raises or is slow is logged and ignored, so a broken exporter cannot fail a job.
- **We do not run evaluations or score anything.** We emit traces that make that trivial on a platform of the user's choice.

## Observers

```python
class Observer(ABC):
    capture_content: ClassVar[bool] = (
        False  # an observer that wants content says so; nothing is built for one that does not
    )

    def job(self, start: JobStart) -> AbstractAsyncContextManager[Scope]: ...
    def attempt(self, start: AttemptStart) -> AbstractAsyncContextManager[Scope]: ...
    async def reused(self, run: ReusedRun) -> None: ...
    async def model_call_started(self, call: ModelCallStart) -> None: ...
    async def model_call_finished(self, call: ModelCallEnd) -> None: ...
    async def tool_call_started(self, call: ToolCallStart) -> None: ...
    async def tool_call_finished(self, call: ToolCallEnd) -> None: ...
    async def submitted(
        self, job: JobSubmitted
    ) -> Mapping[str, str] | None: ...  # the carrier that travels in the message
```

(The exact shape is settled in the issue for the class; this is what it has to cover.)

- **Every method has a default that does nothing.** An observer overrides what it cares about. The class is abstract in the
  same sense as `Database`: it is the interface, with a conformance kit (`testing.check_observer`) that calls every method with
  sample records and checks that none of them fails or changes anything.
- **`job` and `attempt` wrap the work.** The runner enters them around the pipeline and around one attempt, and calls
  `scope.finish(end)` with the outcome before leaving. This is what lets an observer make its span the *current* one while
  the work runs (see [your own spans](#your-own-spans)). `model_call` and `tool_call` are start and finish events: they come
  from a callback in another task, so they cannot wrap anything.
- **Inline, and harmless.** Every call is awaited, with a timeout and with its errors logged and ignored ([the runner](runner.md#observers)).
  A slow observer buffers for itself. OpenTelemetry's own exporters already batch in the background, so ours is cheap.
- **Where they are set.** On the runner (`AgentRunner(db, observers=[...])`: see [the runner](runner.md#observers) for when it calls them), and
  on the client (`JobClient(db, queue, observers=[...])`, for `submitted` only).

## What an observer sees

Records are frozen Pydantic models, as everywhere. Always present (metadata): ids (`job_id`, `agent_run_id`,
`invocation_id`, `parent_agent_run_id`), names and versions, hashes, `attempt` and `max_attempts`, the provider and model,
tokens, timing, status, and the **error type** (never the text). Only for an observer with `capture_content = True`:
the job input and output, the agent input and output, messages, system instructions, tool definitions, tool arguments and
results, and error text.

- **A job** (`JobStart`/`JobEnd`): `job_id`, `name`, `version`, `job_hash`, `input_hash`, `resumable`, `resumed_from`, and the
  trace carrier that came with the message (see [OpenTelemetry](#opentelemetry)); at the end: status, error type.
- **An attempt**: one per row of `agent_runs`. Agent name, version, description and hash, `invocation_id`, `attempt`,
  `parent_agent_run_id` (the agent that launched it), `parent_tool_call_id` (the tool call that launched it), `depth`, provider, model, whether it is a streaming run; at the end: status, tokens, `final` (the
  attempt succeeded, or it was the last one allowed), error type.
- **A reused step** (`ReusedRun`): a step of a resumed job that was answered from the result store, with
  `reused_from_agent_run_id` and the `resumed_from` job. It is not an attempt, and has no model calls.
- **A model call**: provider, model, the model the API reported, the finish reasons, input and output tokens, duration; with content: the messages.
- **A tool call**: tool name and call id (the id the model gave it; the OpenTelemetry tool type is always `function`); with content: arguments and result. A tool that launches an agent (the runner's
  launch tools) has the launched agent's attempt as its child.


## OpenTelemetry

`OTelObserver` ships in the package. It needs `opentelemetry-api` only (the extra `featherweight[otel]`). With no SDK
configured the API does nothing, so it costs nothing to use. The user configures the SDK and the exporter as for any
application.

**The spans** follow the OpenTelemetry [GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai):

| Ours | Span | Kind |
|---|---|---|
| A job | `invoke_workflow {job name}` | internal |
| An attempt of an agent | `invoke_agent {agent name}` | internal |
| A model call | `chat {model}` | client |
| A tool call | `execute_tool {tool name}` | internal |
| A reused step | `invoke_agent {agent name}`, with `ftw.reused` | internal |

- **Nesting:** a job holds the attempts of its pipeline; an attempt holds its model calls and tool calls; a tool that launches
  an agent holds that agent's attempt. The parent of an attempt comes from the tool call that launched it
  (`parent_tool_call_id`) or else from `parent_agent_run_id`, and not only from the current span, because a launched agent runs
  in another task. Retries are siblings that share `ftw.invocation.id`. The job of a [background job](background-jobs.md#events) is under the tool call that started it: the `submitted` hook is told the tool call (`JobSubmitted.parent_agent_run_id` and `parent_tool_call_id`, which are for observers and stored nowhere), and `OTelObserver` gives the carrier of that call's span.
- **Attributes from the conventions:** `gen_ai.operation.name` (`invoke_workflow`, `invoke_agent`, `chat`, `execute_tool`),
  `gen_ai.agent.name` (an agent's name, and a job's name: the conventions call it the name of "the GenAI agent or workflow"),
  `gen_ai.agent.version`, `gen_ai.agent.description`, `gen_ai.request.model`, `gen_ai.tool.name`, `gen_ai.tool.call.id`,
  `gen_ai.tool.type` (always `function`), and `error.type` (the error type, never the text). On a model call also
  `gen_ai.provider.name` (required there, and only there: `gcp.gen_ai` for Google, and `anthropic` and `openai` as they are),
  `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and `gen_ai.response.finish_reasons`. An
  attempt does not repeat the token counts as `gen_ai.usage.*`, so that a platform that sums them over a trace counts each token
  once; its totals are `ftw.tokens.*`.
- **Content, opt-in:** `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`,
  `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, in the JSON structure the conventions give (a job's or an agent's typed
  input and output are the text of one user or assistant message, as JSON). Python span attributes cannot hold an object, so they are
  JSON strings. `gen_ai.tool.definitions` is not emitted: the records do not carry the tool definitions. Content is set only
  when it is on. `OTelObserver(capture_content=...)` decides, and its default follows the
  conventions' own switch, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT`: `NO_CONTENT` (the default) or `SPAN_ONLY` are
  supported; the values that send content as events are not, and are logged and treated as `NO_CONTENT`.
- **Ours, in the `ftw.` namespace:** `ftw.job.id`, `ftw.job.name`, `ftw.job.version`, `ftw.job.hash`, `ftw.job.execution` (1 for the first execution of the job, one more for each takeover: [several workers](submitted-jobs.md#several-workers)), `ftw.agent.hash`,
  `ftw.run.id` (the `agent_run_id`), `ftw.invocation.id`, `ftw.attempt` and `ftw.attempts` (the number, and how many are
  allowed), `ftw.attempt.final`, `ftw.tokens.input` and `ftw.tokens.output` (the attempt's totals), `ftw.reused`,
  `ftw.reused_from_agent_run_id`, `ftw.resumed_from`.
- **A rejected attempt** ends its span with an error status and `error.type` (the exception type, for example the type
  that `check_output` raised). The attempt that counted has `ftw.attempt.final = true`.
- **A reused step** is a span with no model-call children and `ftw.reused = true`, with the ids that say where it came from.
  The ids are how you find the original run; **no span link**, because we do not store span ids.
- **The trace crosses the queue.** `JobClient` asks its observers for a carrier on submit (the W3C `traceparent` and
  `tracestate` of the current span), and the message carries it as `trace_context`. The worker passes it to its observers, so
  that the job's span joins the client's trace. It travels in the message only, and is stored nowhere by us.

**Pinned to the conventions.** The GenAI conventions are in the *Development* status and live in their own repository, with
no release and no schema URL yet, so they will change.

- One constant in the code names the version we follow (`GENAI_CONVENTIONS` in `otel.py`: the repository and the commit that was
  read, now `622b2b6073b15e5ed886c3c12a65a892b9cd5c70` of 2026-11-20, with `CORE_CONVENTIONS`, the core version it refers to),
  and `docs/observability.md` will have a table from each featherweight version to the convention version it follows.
- A test checks every attribute name we emit against `ATTRIBUTES`, a list kept with the code, and the list is updated by hand
  when we move to a newer convention. A change to an attribute name is a breaking change in the changelog.
- The extra pins `opentelemetry-api` to a range. We use the attribute names as strings, so we do not depend on the package that
  carries the constants.

## Your own spans

A span around your own code is yours. You use the OpenTelemetry API directly:

```python
with tracer.start_as_current_span("load documents"):
    docs = await load()
    result = await ctx.run(Summariser, docs)
```

- featherweight offers nothing for it. The one connection is OpenTelemetry's own rule, that a span that is current while your
  code runs is the parent of the spans started inside it. `OTelObserver` makes the job span and the attempt span current while the
  work runs (this is what the wrapping hooks are for), so your span is under the job, and our attempts are under your span.
- Without `OTelObserver` there is no job span, and your spans are simply roots. That is not an error.
- A tool that you write and that starts a span nests under the attempt's tool span only when the callback runs in the tool's own
  context; the guide says that your own spans inside a tool may be attached to the attempt and not to the tool call.

## Evaluation

Evaluation platforms read traces. For a trace to be useful to them it needs the path and the content, which is why model calls
and tool calls have their own spans, and why content is an opt-in switch and not a different feature.

- **To evaluate the answer:** turn content on. The span of an attempt has the input and the final output; the model call has the messages.
- **To evaluate the path:** the tool calls, in order, with their arguments and results, under the attempt that made them.
- **To tell which attempt counted:** `ftw.attempt.final`, and `error.type` on the ones that were rejected.
- **To compare versions:** the agent name, version and hash, the job name and version ([a job's version](jobs-and-tracing.md#running-a-job-class)),
  and the model are on the spans. The code release is the OpenTelemetry resource attribute `service.version`, which the user sets (a git sha
  or a tag), and which evaluation platforms already group by.
- We do not run evaluations, score, or have a feedback API.

## What changes

- **New:** `Observer` and its records, `testing.check_observer`, `OTelObserver`, and the extra `featherweight[otel]`.
- **Elsewhere,** each in the spec that owns it: the runner calls observers ([runner](runner.md#observers)); `JobMessage.trace_context` and
  `JobClient(observers=)` ([submitted jobs](submitted-jobs.md#the-queue)); `run_job` runs a job class, and the job version
  ([jobs and tracing](jobs-and-tracing.md#running-a-job-class)); `SqliteDatabase(":memory:")` and `jobs.job_version` ([persistence](persistence.md)).
- **Not changed:** the run log's tables for attempts, the result store, resuming, the job events, SSE.

## Decisions

- **One concept, observers; OpenTelemetry is one of them,** and the one we ship.
- **Inline calls, with a timeout and no failures.** Simple, ordered, and it is what lets a span be current while the work runs.
- **Metadata always, content on request,** by a flag on the observer, so nothing is built for content that nobody wants.
- **The GenAI conventions, with our own attributes under `ftw.`,** so that the platforms read our traces on day one.
- **Spans for model calls and tool calls,** because evaluation needs the path. The cost is the callback handler.
- **No trace or span ids in the run log.** They are the application's. The reuse reference is the ids we already store.
- **No API for your own spans.** The principle is a clean line between what we offer and what you do.
- **Observers are on the runner,** so a job run by `run_job` and a job run by a worker report the same way, and nothing is reported twice.

## Not supported

- **Running evaluations,** scoring, datasets, or a feedback API.
- **A featherweight API for spans around your own code,** or span links to a reused run's original span.
- **Content as events** (the conventions' `EVENT_ONLY` and `SPAN_AND_EVENT`), and per-token events for a streaming run.
- **Metrics and logs** through OpenTelemetry. Spans only. The logger lines stay as they are.
- **Storing any trace data,** and an observer that changes a run.
- **A different tracing library** than OpenTelemetry as a product feature. Write an observer.

## Tests

No live model call. Fake models (with tool calls), `MemoryQueue`, an in-memory span exporter from the OpenTelemetry SDK (a dev
dependency only).

- **Observers:** every hook is called in order and with the right records; an observer that raises, or is too slow, changes
  nothing; content is built only when an observer asks; `check_observer` against a no-op observer and a recording one.
- **The callback handler:** model calls and tool calls of a fake model, with timings, and with a launched agent under its tool call.
- **OpenTelemetry:** the span tree of a job with retries, a launched agent and a reused step; every attribute name against the
  list; content off and on; the status and `error.type` of a rejected attempt; `ftw.attempt.final`; the trace context from a
  client to a worker; your own span inside a job; no SDK means no spans and no errors.

## The work

Tracked in [epic #90](https://github.com/aadhithya/featherweight/issues/90). One issue and one pull request each, in this order. The first,
[#91](https://github.com/aadhithya/featherweight/issues/91), is specified in [jobs and tracing](jobs-and-tracing.md#running-a-job-class), and the others depend on it:

1. [#91](https://github.com/aadhithya/featherweight/issues/91) **`run_job` runs a job class** (built; specified in the jobs spec, as above).
2. [#92](https://github.com/aadhithya/featherweight/issues/92) **The `Observer` class,** its records, `check_observer`, the runner's calls, the callback handler for model and tool calls, and the content switch.
3. [#93](https://github.com/aadhithya/featherweight/issues/93) **Jobs, and the trace across the queue:** the job scope for `run_job` and the worker, `JobMessage.trace_context`, `JobClient` observers, the queue kit.
4. [#94](https://github.com/aadhithya/featherweight/issues/94) **`OTelObserver`:** the GenAI mapping, the `ftw.` attributes, the pinned version and its test, `featherweight[otel]`.
5. [#95](https://github.com/aadhithya/featherweight/issues/95) **The guide, an example, the skill,** and dropping "OpenTelemetry? Too big" from the README.
