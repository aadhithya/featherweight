<!-- nav:top -->
[Book](../README.md) › [Part 5: In production](../README.md#part-5-in-production) › Observability  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# Observability

See what it did as OpenTelemetry traces.

Record the run as OpenTelemetry spans, and add a span of your own.

<!-- code: code/5-4/app.py -->
```python
"""Chapter 5-4: the run as OpenTelemetry spans, with a span of your own. Collected in memory here, printed as a tree."""

import asyncio
import sys
from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, OTelObserver, SqliteDatabase, job, run_job

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(
    SimpleSpanProcessor(exporter)
)  # a real app exports to its backend: OTLP, Jaeger, a platform
trace.set_tracer_provider(provider)


class ReportFile(BaseModel):
    path: str


@job  # a class made from this function: a name, a version, a typed input and output
async def summarise_file(ctx: JobContext, input: ReportFile) -> Summary:
    with trace.get_tracer("incident_app").start_as_current_span("read the report"):  # your span, under the job's
        report = Report(text=Path(input.path).read_text())
    result = await ctx.run(IncidentSummariser, report)
    if result.output is None:
        raise RuntimeError(result.error)
    return result.output


async def main(path: str = "reports/01-checkout-outage.txt") -> None:
    async with SqliteDatabase(":memory:") as db:
        runner = AgentRunner(db, observers=[OTelObserver()])  # content stays off: capture_content=True turns it on
        await run_job(runner, summarise_file, ReportFile(path=path))
    depth: dict[int, int] = {}
    for span in sorted(exporter.get_finished_spans(), key=lambda s: s.start_time or 0):
        depth[span.context.span_id] = depth[span.parent.span_id] + 1 if span.parent else 0
        print("  " * depth[span.context.span_id] + span.name)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
```

```text
invoke_workflow summarise_file
  read the report
  invoke_agent incident_summariser
    chat gemini-3.5-flash-lite
```

**What to notice**

- **`OTelObserver`** is the observer that ships, and observers are classes: yours is [Part 6](../06-bring-your-own-storage/05-an-observer.md). It goes on the runner. featherweight uses only the OpenTelemetry **API**: you configure the SDK and the exporter for your backend, as for any application
  (here an in-memory one, so that the book can print the tree; a real app exports with OTLP to its platform). Without an SDK it records nothing and costs nothing.
- **The spans** follow the OpenTelemetry GenAI conventions, so platforms read them as they are:

| What | Span |
|---|---|
| A job | `invoke_workflow {job}` |
| An attempt of an agent | `invoke_agent {agent}` |
| A model call | `chat {model}` |
| A tool call | `execute_tool {tool}` |

- **Your own span** is under the job's, and the agent's attempts are siblings of it, because `OTelObserver` makes the job's span the current one while the job runs.
- **Content is off.** The spans carry names, ids, status, tokens and timing, and no input, output or messages. `OTelObserver(capture_content=True)` (or `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT=SPAN_ONLY`)
  puts the content on the spans: it goes only where your exporter sends it, and featherweight keeps none of it.
- **Across the queue** a submitted job and its worker form one trace: give the client the observer too, `JobClient(db, queue, observers=[OTelObserver()])`.
- **Evaluation** is done on the traces you collect, on a platform of your choice. featherweight does not evaluate or score: it emits traces that carry the path (the model and tool calls, in order),
  which attempt counted (`ftw.attempt.final`) and the versions, so that a platform can.

> **Why an observer.** It never changes a run: one that raises or is slow is logged and ignored. See [observers and OpenTelemetry](../../specs/observability.md) and [the guide](../../observability.md).

Next: bring your own storage.

<!-- nav:bottom -->
---

[← Background agents](03-background-agents.md) · [Part 5: In production](../README.md#part-5-in-production) · [Who is asking →](05-who-is-asking.md)
<!-- /nav:bottom -->
