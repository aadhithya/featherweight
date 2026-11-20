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
