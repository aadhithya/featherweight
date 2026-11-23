# Traces for jobs and agents

```python
from featherweight import AgentRunner, OTelObserver

runner = AgentRunner(
    db, observers=[OTelObserver()]
)  # pip install 'featherweight[otel]'; you configure the SDK and the exporter
```

A job becomes `invoke_workflow`, each attempt `invoke_agent`, with `chat` and `execute_tool` spans under it, in the GenAI conventions.
Content is off unless `OTelObserver(capture_content=True)`. Your own spans use the OpenTelemetry API and nest under the job.
[the observability guide](../observability.md) has the rest, and `examples/observability` runs it.
