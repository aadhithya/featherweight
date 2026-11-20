# Spec: observers, OpenTelemetry and one way to run a job

Status: designed, not built. It is tracked in an epic (listed [at the end](#the-work)), and each part has its own issue. When
a part is built, this page says so, and the guide `docs/observability.md` shows how to use it.

It builds on [the persistence spec](persistence.md) (the rule that featherweight keeps metadata only, and that content never
goes into the run log, even by option) and [submitted jobs](submitted-jobs.md) (jobs, the worker, the queue). It changes
[jobs and tracing](jobs-and-tracing.md) (`run_job` runs a job class, the job version), [the runner](runner.md) (observers, and
the callback that sees model calls and tool calls) and [submitted jobs](submitted-jobs.md) (the trace context on the message).

- [Goal](#goal)
- [The principle](#the-principle)
- [One way to run a job](#one-way-to-run-a-job)
- [Observers](#observers)
- [What an observer sees](#what-an-observer-sees)
- [OpenTelemetry](#opentelemetry)
- [Your own spans](#your-own-spans)
- [Evaluation](#evaluation)
- [Versions](#versions)
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
- **One way to run a job:** a job is a declared class, whether a worker runs it or you await it in your process, so that a job
  has one definition, one version and one trace shape.

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

## One way to run a job

Today a job is two things: a function you pass to `run_job`, and a `Job` class that you submit. This spec makes it one.

```python
@job
async def summarise(ctx: JobContext, input: In) -> Out: ...


outcome = await run_job(runner, summarise, In(...))  # in your process: you await it
job_id = await client.submit(summarise, In(...))  # on a worker: you follow it
```

- **`run_job(runner, job, input, *, job_id=None, resume_from=None, resumable=False)`** runs a `Job` class (or an agent class,
  as `submit` does) in your process and returns a `JobOutcome` with the typed output. A plain function is no longer accepted: `@job`
  makes a job from a function in three lines, and the input and output become typed. It needs the runner (a job's `run` gets
  a `JobContext` that holds it), and the runner holds the database.
- **Parity.** The only difference between running a job in your process and on a worker is **who runs it, and where**. There
  is one function, used by `run_job` and by the worker, that starts the job (`start_job`), runs it, writes the result, sets the
  final status and writes the events. So both:
  - write the same job row, the same hashes and the same events (`events()` and the traces cannot tell them apart);
  - write the result to the runner's result store **if there is one**, and keep nothing if `results=None` (the caller still
    gets the output back from `run_job`);
  - resume the same way (`resume_from`, `resumable`);
  - fail the same way: a job that raises is a `FAILED` outcome, a cancelled task is `CANCELLED`.
- **What stays different:** a submitted job's input is JSON, because it crosses a queue; in your process it is the model as
  you built it. A submitted job is cancelled through the database (`client.cancel`), and one in your process by cancelling
  its task.
- **Without a database.** `SqliteDatabase(":memory:")` runs everything in memory, with no file and no service: the same code,
  the same migrations. The job row, the events and the default result store live in it, and are gone when it closes. It is the
  quick way to try a job, and the way to test one.
- **Streaming agents** cannot be jobs, as before.

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
- **Inline.** Every call is awaited by the runner, with a timeout (a second by default) and with its errors logged and
  ignored. A slow observer buffers for itself. OpenTelemetry's own exporters already batch in the background, so ours is
  cheap.
- **Where they are set.** `AgentRunner(db, observers=[...])`. A worker runs its jobs with its runner, so it has them too, and
  a job run by `run_job` and one run by a worker call the same observers. `JobClient(db, queue, observers=[...])` takes
  observers for `submitted` only.
- **Streaming runs** call the same hooks for the start and the end of the run, the model calls and the tool calls, and none per
  token.

## What an observer sees

Records are frozen Pydantic models, as everywhere. Always present (metadata): ids (`job_id`, `agent_run_id`,
`invocation_id`, `parent_agent_run_id`), names and versions, hashes, `attempt` and `max_attempts`, the provider and model,
tokens, timing, status, and the **error type** (never the text). Only for an observer with `capture_content = True`:
the job input and output, the agent input and output, messages, system instructions, tool definitions, tool arguments and
results, and error text.

- **A job** (`JobStart`/`JobEnd`): `job_id`, `name`, `version`, `job_hash`, `input_hash`, `resumable`, `resumed_from`, and the
  trace carrier that came with the message (see [OpenTelemetry](#opentelemetry)); at the end: status, error type.
- **An attempt**: one per row of `agent_runs`. Agent name, version, description and hash, `invocation_id`, `attempt`,
  `parent_agent_run_id` (the agent that launched it), `depth`, provider, model; at the end: status, tokens, `final` (the
  attempt succeeded, or it was the last one allowed), error type.
- **A reused step** (`ReusedRun`): a step of a resumed job that was answered from the result store, with
  `reused_from_agent_run_id` and the `resumed_from` job. It is not an attempt, and has no model calls.
- **A model call**: model, request settings, the finish reasons, input and output tokens; with content: the messages.
- **A tool call**: tool name, call id, type; with content: arguments and result. A tool that launches an agent (the runner's
  launch tools) has the launched agent's attempt as its child.

**How the runner sees model calls and tool calls.** An attempt is one `lc_agent.ainvoke(...)`: the model calls and tool calls
happen inside LangChain's agent loop. A callback handler, added to the config of each attempt (and of a streamed run), turns
LangChain's callbacks (`on_chat_model_start/end`, `on_tool_start/end`) into the observer's events, with real timings. It is the
one place that depends on LangChain's callback interface, and it does nothing when there are no observers.

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
  an agent holds that agent's attempt. The parent of an attempt comes from `parent_agent_run_id`, and not only from the
  current span, because a launched agent runs in another task. Retries are siblings that share `ftw.invocation.id`.
- **Attributes from the conventions:** `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.agent.name`,
  `gen_ai.agent.version`, `gen_ai.agent.description`, `gen_ai.request.model`, `gen_ai.response.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.tool.name`,
  `gen_ai.tool.call.id`, `gen_ai.tool.type`, and `error.type` (the error type, never the text).
- **Content, opt-in:** `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`,
  `gen_ai.tool.definitions`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, in the JSON structure the conventions
  give. They are set only when content is on. `OTelObserver(capture_content=...)` decides, and its default follows the
  conventions' own switch, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT`: `NO_CONTENT` (the default) or `SPAN_ONLY` are
  supported; the values that send content as events are not, and are logged and treated as `NO_CONTENT`.
- **Ours, in the `ftw.` namespace:** `ftw.job.id`, `ftw.job.name`, `ftw.job.version`, `ftw.job.hash`, `ftw.agent.hash`,
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

- One constant in the code names the version we follow (the repository and its commit), and `docs/observability.md` has a
  table from each featherweight version to the convention version it follows.
- A test checks every attribute name we emit against a list kept with the code, and the list is updated by hand when we move
  to a newer convention. A change to an attribute name is a breaking change in the changelog.
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
- **To compare versions:** the agent name, version and hash, the job name and version, and the model are on the spans. The
  code release is the OpenTelemetry resource attribute `service.version`, which the user sets (a git sha or a tag), and which
  evaluation platforms already group by.
- We do not run evaluations, score, or have a feedback API.

## Versions

- **A job has a version**, as an agent has: `Job.version`, set by hand when the pipeline's behaviour changes. `@job` takes
  `version=` (default `"1"`). It is stored on the job (`jobs.job_version`) and is on the job's span as `ftw.job.version`.
- **A pipeline's code is not hashed.** Arbitrary Python cannot be fingerprinted in a way that means anything (closures,
  imports, everything they call). `job_hash` covers the declaration: name, version and the two models.
- The deterministic code around the agents is versioned by the job's `version`, and by `service.version`, as above.

## What changes

- **`run_job`** takes a runner, a job class and an input (see [one way to run a job](#one-way-to-run-a-job)). One function runs
  a job, for it and for the worker. Examples, docs and tests that passed a function now declare a job.
- **`Database`**: `jobs.job_version` (the baseline migration is edited, as nothing is released). `SqliteDatabase(":memory:")` works.
- **`JobMessage`** gets `trace_context: dict[str, str]` (empty by default); `check_queue` checks that a queue carries it.
- **`AgentRunner`** gets `observers`, a callback handler for each attempt and each streamed run, and the calls to the observers.
  `JobClient` gets `observers`.
- **New:** `Observer` and its records, `testing.check_observer`, `OTelObserver`, `featherweight[otel]`.
- **Not changed:** the run log's tables for attempts, the result store, resuming, the job events, SSE.

## Decisions

- **One concept, observers; OpenTelemetry is one of them,** and the one we ship.
- **Inline calls, with a timeout and no failures.** Simple, ordered, and it is what lets a span be current while the work runs.
- **Metadata always, content on request,** by a flag on the observer, so nothing is built for content that nobody wants.
- **The GenAI conventions, with our own attributes under `ftw.`,** so that the platforms read our traces on day one.
- **Spans for model calls and tool calls,** because evaluation needs the path. The cost is the callback handler.
- **No trace or span ids in the run log.** They are the application's. The reuse reference is the ids we already store.
- **No API for your own spans.** The principle is a clean line between what we offer and what you do.
- **A job is a class, in your process or on a worker;** the version of a job is the version of the pipeline, and the pipeline
  code is not hashed.
- **A plain function is no longer a job for `run_job`;** `@job` is the short way to declare one.
- **Observers are on the runner,** so a job run by `run_job` and a job run by a worker report the same way, and nothing is reported twice.

## Not supported

- **Running evaluations,** scoring, datasets, or a feedback API.
- **A featherweight API for spans around your own code,** or span links to a reused run's original span.
- **Content as events** (the conventions' `EVENT_ONLY` and `SPAN_AND_EVENT`), and per-token events for a streaming run.
- **Metrics and logs** through OpenTelemetry. Spans only. The logger lines stay as they are.
- **Storing any trace data,** and an observer that changes a run.
- **A job that is a plain function in `run_job`.**
- **A different tracing library** than OpenTelemetry as a product feature. Write an observer.

## Tests

No live model call. Fake models (with tool calls), `MemoryQueue`, an in-memory span exporter from the OpenTelemetry SDK (a dev
dependency only).

- **`run_job` and the worker:** the same job, run both ways, gives the same row, hashes, events and result; `:memory:`;
  `results=None`; resume; a failing and a cancelled job.
- **Observers:** every hook is called in order and with the right records; an observer that raises, or is too slow, changes
  nothing; content is built only when an observer asks; `check_observer` against a no-op observer and a recording one.
- **The callback handler:** model calls and tool calls of a fake model, with timings, and with a launched agent under its tool call.
- **OpenTelemetry:** the span tree of a job with retries, a launched agent and a reused step; every attribute name against the
  list; content off and on; the status and `error.type` of a rejected attempt; `ftw.attempt.final`; the trace context from a
  client to a worker; your own span inside a job; no SDK means no spans and no errors.

## The work

Tracked in an epic. One issue and one pull request each, in this order:

1. **`run_job` runs a job class,** one function for `run_job` and the worker, `:memory:`, `job_version`; examples, docs and tests converted.
2. **The `Observer` class,** its records, `check_observer`, the runner's calls, the callback handler for model and tool calls, and the content switch.
3. **Jobs, and the trace across the queue:** the job scope for `run_job` and the worker, `JobMessage.trace_context`, `JobClient` observers, the queue kit.
4. **`OTelObserver`:** the GenAI mapping, the `ftw.` attributes, the pinned version and its test, `featherweight[otel]`.
5. **The guide, an example, the skill,** and dropping "OpenTelemetry? Too big" from the README.
