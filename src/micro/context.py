"""Per-attempt context the runner hands to tools through langchain's `ToolRuntime`.

Tools declare a `runtime: ToolRuntime[RunContext]` parameter; langchain fills it in and hides it
from the model, so tools stay plain static objects that can sit in an agent's allow-list.
"""

from contextvars import ContextVar
from dataclasses import dataclass

from micro.store import RunStore


@dataclass
class RunContext:
    store: RunStore
    job_id: str | None
    agent_run_id: str  # the attempt that is running now


# The attempt that is running in this task. Tools that are built once and shared by many attempts (the MCP tools) read
# it to say, in the log, which attempt called them.
CURRENT_AGENT_RUN_ID: ContextVar[str | None] = ContextVar("current_agent_run_id", default=None)
