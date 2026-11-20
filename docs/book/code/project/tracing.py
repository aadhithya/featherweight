"""The traces (Part 5): collected in memory, and printed in a few lines. A real app exports them to its backend."""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Exporters: console, OTLP (to Jaeger, Langfuse, Phoenix and others) or, as here, in memory, so that the spans can be counted.
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)  # set it before an OTelObserver is made


def print_trace() -> None:
    spans = exporter.get_finished_spans()
    print("trace:")
    for kind in ("invoke_workflow", "invoke_agent", "chat", "execute_tool"):
        print(f"  {sum(1 for s in spans if s.name.startswith(kind))} {kind} spans")
