# Traces for jobs and agents

See what a job and its agents did, as OpenTelemetry spans. The example prints them to the console; point the exporter at your platform to keep them.

```python
import asyncio

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from featherweight import AgentRunner, OTelObserver, SqliteDatabase, default_db_path, run_job

# PeopleExtractor and ExtractInput: the agent of Getting started (../getting-started.md#an-agent)


async def main() -> None:
    provider = TracerProvider()  # you configure the SDK and the exporter
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    async with SqliteDatabase(default_db_path()) as db:
        runner = AgentRunner(db, observers=[OTelObserver(tracer_provider=provider)])
        await run_job(runner, PeopleExtractor, ExtractInput(text="Anna Berger met Tom Okafor on Monday."))


if __name__ == "__main__":
    asyncio.run(main())
```

`pip install "featherweight[otel]"`. A job becomes `invoke_workflow`, each attempt `invoke_agent`, with `chat` and `execute_tool` spans under it, in the GenAI conventions. Content is off unless `OTelObserver(capture_content=True)`. Your own spans use the OpenTelemetry API and nest under the job. [The observability guide](../observability.md) has the rest, and `examples/observability` runs it.
