# Tracing a job

A job traced with OpenTelemetry, and a span of your own inside it. No model and no API key: the job is plain code.

```
make example-observability
```

[`trace.py`](trace.py) gives the runner an `OTelObserver`, runs a job, and prints the spans to the console. You will see
`invoke_workflow shout` (the job), and `make it loud` (your span, started with the OpenTelemetry API) under it. With an agent in
the job there would be an `invoke_agent`, `chat` and `execute_tool` span for every attempt, model call and tool call.

A real app configures the SDK for its own backend, as for any application: featherweight only uses the OpenTelemetry API. The
[guide](../../docs/observability.md) has what is on the spans, how to turn on content, and what to give an evaluation platform.
Without Docker: `uv run python examples/observability/trace.py`.
