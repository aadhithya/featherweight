<!-- Copied from docs/observability.md by scripts/build_skill.py. Edit the original, not this file. -->

# Observability

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

See what your jobs and agents do, in the tool you already use, and run evaluations on the traces you collect. featherweight emits
the spans. Where they go, and what you do with them, is yours.

Spec: [Observers and OpenTelemetry](https://github.com/aadhithya/featherweight/blob/main/docs/specs/observability.md). Example: `examples/observability` (`make example-observability`).

- [How it works](#how-it-works)
- [OpenTelemetry](#opentelemetry)
- [What is on the spans](#what-is-on-the-spans)
- [Content](#content)
- [Your own spans](#your-own-spans)
- [Across the queue](#across-the-queue)
- [Evaluation](#evaluation)
- [Writing your own observer](#writing-your-own-observer)
- [Versions of the conventions](#versions-of-the-conventions)
- [What is not here](#what-is-not-here)

## How it works

An **observer** is a class that the runner calls as things happen: a job starts and ends, an attempt starts and ends, a step is
reused, a model call or a tool call starts and ends. featherweight stores nothing that an observer receives, and an observer never
changes a run: one that raises or is too slow is logged and ignored.

```python
runner = AgentRunner(db, observers=[OTelObserver()])
```

A job run by `run_job` and a job run by a worker report the same, because the worker uses its runner. Without observers nothing
changes.

**Metadata always, content on request.** An observer always receives ids, names, status, tokens, timing, hashes and the type of an
error. The inputs, outputs, messages, tool arguments and results, and the text of an error go only to an observer that asks for them.

## OpenTelemetry

`OTelObserver` is the one we ship, because every observability and evaluation platform reads OpenTelemetry.

```
pip install 'featherweight[otel]'      # opentelemetry-api only
```

```python
from featherweight import AgentRunner, OTelObserver

runner = AgentRunner(db, observers=[OTelObserver()])
```

featherweight uses the OpenTelemetry **API** only. You configure the SDK and the exporter for your backend, as for any application
(`examples/observability` prints to the console). Without an SDK it does nothing and costs nothing.

## What is on the spans

The spans follow the OpenTelemetry [GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai), so
the platforms read them as they are.

| What | Span | Under |
|---|---|---|
| A job | `invoke_workflow {job name}` | the span of whoever submitted it, if there is one |
| An attempt of an agent | `invoke_agent {agent name}` | the job, or the tool call that launched the agent |
| A model call | `chat {model}` | its attempt |
| A tool call | `execute_tool {tool name}` | its attempt |
| A step reused by a resumed job | `invoke_agent {agent name}`, with `ftw.reused` | the job |

- A retry is a span of its own, a sibling of the attempt it follows. They share `ftw.run.id`, and the attempt that counted has
  `ftw.attempt.final = true`. A rejected attempt ends with an error status and `error.type`: the type of the exception, and never its text.
- A reused step has no model calls under it. `ftw.reused_from_attempt_id` and `ftw.resumed_from` say where it came from, so you can
  find the original run.
- The conventions' attributes are used for what they cover (the operation, the agent and its version, the model, the tokens, the finish
  reasons, the tool name and call id). **Ours are in the `ftw.` namespace:** the job's id, name, version and hash, the agent's hash, the
  attempt number and how many are allowed, an attempt's token totals, and the ids that link a reused step. The full list is in the
  [spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/observability.md#opentelemetry).

## Content

Off by default. To put the messages, the tool arguments and results, and the typed input and output of jobs and agents on the spans:

```python
OTelObserver(capture_content=True)
```

or set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT=SPAN_ONLY`, the convention's own switch (an explicit `capture_content` wins). The
content is set as the conventions' attributes (`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.tool.call.arguments`, ...),
as JSON strings. It will contain whatever your users sent, so it goes where you decide: your exporter and your backend, and nowhere
that featherweight keeps. The values that send content as events are not supported, and are treated as off.

## Your own spans

A span around your own code is yours: use the OpenTelemetry API, and featherweight offers nothing for it.

```python
tracer = trace.get_tracer("my_app")


@job
async def summarise(ctx: JobContext, input: In) -> Out:
    with tracer.start_as_current_span("load documents"):
        docs = await load(input)
    return await ctx.run(Summariser, docs)
```

`OTelObserver` makes the job's span and each attempt's span the current one while the work runs, so your span is under the job, and the
attempts started inside your span are under it. That is OpenTelemetry's own rule, not a featherweight feature. Without `OTelObserver`
there is no job span, and your spans are simply roots.

## Across the queue

A job that a client submits and a worker runs is one trace. `JobClient(db, queue, observers=[OTelObserver()])` puts the W3C trace
context in the message, and the worker's job span joins it. Both sides need an observer; the context travels in the message only,
and is stored in no table.

## Evaluation

Evaluation platforms (Langfuse, Phoenix and others) read traces. featherweight does not run evaluations or score anything: it emits
traces that make that easy.

- **To judge the answer:** turn content on. The job and attempt spans have the input and the final output, and the model calls have the messages.
- **To judge the path:** the tool calls are spans, in order, with their arguments and results (with content on), under the attempt that made them.
- **To know which attempt counted:** `ftw.attempt.final`, and `error.type` on the rejected ones.
- **To compare versions:** the agent's name, version and hash, the job's name and version, and the model are on the spans. Set the
  OpenTelemetry resource attribute `service.version` to your release (a git sha or a tag) for the code around them: platforms group by it.

## Writing your own observer

Subclass `Observer` and override only what you need: every method does nothing by default.

```python
from featherweight import ModelCallEnd, Observer


class Tokens(Observer):
    def __init__(self):
        self.total = 0

    async def model_call_finished(self, call: ModelCallEnd) -> None:
        self.total += (call.input_tokens or 0) + (call.output_tokens or 0)
```

The job and attempt hooks (`job`, `attempt`) wrap the work, so an observer can keep a context (a span) open while it runs. Set
`capture_content = True` on the class to receive the content fields. Check yours with the kit:

```python
await testing.check_observer(lambda: Tokens())
```

Calls are made inline, so an observer that does slow work (a network call per event) should buffer for itself.

## Versions of the conventions

The GenAI conventions are in the *Development* status, so attribute names may change. featherweight pins the version it follows, and a
test checks every attribute it emits against a list kept with the code. A rename is a breaking change in the changelog.

| featherweight | GenAI conventions |
|---|---|
| 0.1.0 | [`semantic-conventions-genai@622b2b6`](https://github.com/open-telemetry/semantic-conventions-genai/tree/622b2b6073b15e5ed886c3c12a65a892b9cd5c70), core conventions 1.45.0 |

## What is not here

- Running evaluations, scoring, datasets, or a feedback API.
- Metrics and logs through OpenTelemetry: spans only.
- Content as events, and an event per token for a streaming run.
- `gen_ai.tool.definitions`, and a span link from a reused step to its original span: we store no trace or span ids.
- A tracing helper for your own code: use the OpenTelemetry API.
