"""Per-attempt context the runner hands to tools through langchain's `ToolRuntime`.

Tools declare a `runtime: ToolRuntime[RunContext]` parameter; langchain fills it in and hides it
from the model, so tools stay plain static objects that can sit in an agent's allow-list.
"""

from dataclasses import dataclass

from micro.store import RunStore


@dataclass
class RunContext:
    store: RunStore
    job_id: str | None
    agent_run_id: str  # the attempt that is running now
