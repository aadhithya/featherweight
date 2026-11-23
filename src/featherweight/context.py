"""Per-attempt context the runner hands to tools through langchain's `ToolRuntime`.

Tools declare a `runtime: ToolRuntime[RunContext]` parameter; langchain fills it in and hides it
from the model, so tools stay plain static objects that can sit in an agent's allow-list.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

IdentityT = TypeVar("IdentityT")


@dataclass
class RunContext(Generic[IdentityT]):
    """What a tool can ask for with `runtime: ToolRuntime[RunContext[Teacher]]`: LangChain fills it in and hides it from the model."""

    job_id: str | None
    agent_run_id: str  # the attempt that is running now
    identity: IdentityT | None = None  # who the run is for, if the application gave one (docs/specs/identity.md)


# Who the run in this task is for: the identity the application passed to `run`, `run_many`, `stream` or `run_job`. A launched agent and an
# agent started in the background here inherit it, because they run in tasks made from this one. For code that cannot take `runtime`
# (the shared MCP tools). It is never logged, hashed or stored.
CURRENT_IDENTITY: ContextVar[Any] = ContextVar("current_identity", default=None)

# The attempt that is running in this task. Tools that are built once and shared by many attempts (the MCP tools) read
# it to say, in the log, which attempt called them.
CURRENT_AGENT_RUN_ID: ContextVar[str | None] = ContextVar("current_agent_run_id", default=None)

# The job and the execution of it that this task runs, while a job runs (set by `execute_job`). A run records the execution that made it
# from this, and not from the job's row: a worker that lost the job and has not noticed yet is still the execution it was.
CURRENT_JOB_EXECUTION: ContextVar[tuple[str, int] | None] = ContextVar("current_job_execution", default=None)

# True while a job that was started as a background agent runs (set by `execute_job` from the job's `parent_agent_run_id`): what it
# runs, inline sub-agents included, cannot start background agents of its own.
IN_BACKGROUND_JOB: ContextVar[bool] = ContextVar("in_background_job", default=False)

# The tool call that is running in this task, while observers listen: the callback of the attempt sets it, so that an agent launched by
# the tool can say which call launched it. It is `None` when there are no observers.
CURRENT_TOOL_CALL_ID: ContextVar[str | None] = ContextVar("current_tool_call_id", default=None)
