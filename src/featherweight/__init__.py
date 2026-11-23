"""featherweight: an opinionated agent harness. Pydantic is the spec.

    from featherweight import AgentBase, AgentRunner

    class Summariser(AgentBase): ...
    runner = AgentRunner(db)

The public names are available on the package. Each is loaded when it is first used, so that `import featherweight` stays
light and imports no model provider. The modules are there too (`featherweight.model_settings`, ...), and the two adapters are `featherweight.adapters.agui` and `featherweight.adapters.vercel`.
"""

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"  # x-release-please-version

# The public API: name -> the module that defines it.
_EXPORTS = {
    # agents and results
    "AgentBase": "models",
    "AgentInput": "models",
    "AgentOutput": "models",
    "AgentResult": "models",
    "RunStatus": "models",
    "AgentRunner": "runner",
    # streaming agents
    "StreamingAgent": "streaming",
    "StreamingRun": "streaming",
    "ChatInput": "streaming",
    "to_langchain": "messages",
    "to_agui": "messages",
    # models
    "ModelSettings": "model_settings",
    "ModelInfo": "model_settings",
    "Gemini": "model_settings",
    "Gemma": "model_settings",
    "Claude": "model_settings",
    "OpenAI": "model_settings",
    # the run log and jobs
    "Database": "db",
    "SqliteDatabase": "sqlite",
    "PostgresDatabase": "postgres",
    "RunStart": "db",
    "RunEnd": "db",
    "JobRow": "db",
    "JobOwner": "db",
    "JobLost": "db",
    "AgentRunRow": "db",
    "JobEvent": "db",
    "job_events": "events",
    "wait_for_job": "events",
    "default_db_path": "db",
    "run_job": "jobs",
    "JobWorker": "worker",
    # observers
    "Observer": "observers",
    "Scope": "observers",
    "JobStart": "observers",
    "JobEnd": "observers",
    "JobSubmitted": "observers",
    "AttemptStart": "observers",
    "AttemptEnd": "observers",
    "ReusedRun": "observers",
    "ModelCallStart": "observers",
    "ModelCallEnd": "observers",
    "ToolCallStart": "observers",
    "ToolCallEnd": "observers",
    "OTelObserver": "otel",
    "JobOutcome": "jobs",
    "Job": "jobs",
    "JobClient": "client",
    "job": "jobs",
    "JobContext": "jobs",
    "JobTimeout": "jobs",
    "as_job": "jobs",
    # what an agent can have, and what a tool can use
    "Skill": "skills",
    "StdioMCPServer": "mcp",
    "HttpMCPServer": "mcp",
    "ResultStore": "results",
    "ResultKey": "results",
    "SqliteResultStore": "results",
    "JobQueue": "queue",
    "JobMessage": "queue",
    "Delivery": "queue",
    "SqliteQueue": "queue",
    "RunContext": "context",
    "SetupError": "errors",
    "BadRequest": "adapters.errors",
    "setup_logging": "log",
}
_SUBMODULES = {
    "agui": "adapters.agui",
    "vercel": "adapters.vercel",
    "sse": "sse",
}  # the two adapters, as `featherweight.agui` and `featherweight.vercel`
_MODULES = (
    "adapters",
    "client",
    "context",
    "db",
    "errors",
    "events",
    "identity",
    "jobs",
    "log",
    "mcp",
    "messages",
    "model_settings",
    "models",
    "postgres",
    "queue",
    "registry",
    "results",
    "runner",
    "skills",
    "sqlite",
    "streaming",
    "testing",
    "tools",
    "worker",
)

__all__ = ["__version__", *_EXPORTS, *_SUBMODULES]

if TYPE_CHECKING:  # for editors and type checkers: the same names, imported
    from featherweight import adapters as adapters
    from featherweight import sse as sse
    from featherweight.adapters import agui as agui
    from featherweight.adapters import vercel as vercel
    from featherweight.adapters.errors import BadRequest as BadRequest
    from featherweight.client import JobClient as JobClient
    from featherweight.context import RunContext as RunContext
    from featherweight.db import AgentRunRow as AgentRunRow
    from featherweight.db import Database as Database
    from featherweight.db import JobEvent as JobEvent
    from featherweight.db import JobLost as JobLost
    from featherweight.db import JobOwner as JobOwner
    from featherweight.db import JobRow as JobRow
    from featherweight.db import RunEnd as RunEnd
    from featherweight.db import RunStart as RunStart
    from featherweight.db import default_db_path as default_db_path
    from featherweight.errors import SetupError as SetupError
    from featherweight.events import job_events as job_events
    from featherweight.events import wait_for_job as wait_for_job
    from featherweight.jobs import Job as Job
    from featherweight.jobs import JobContext as JobContext
    from featherweight.jobs import JobOutcome as JobOutcome
    from featherweight.jobs import JobTimeout as JobTimeout
    from featherweight.jobs import as_job as as_job
    from featherweight.jobs import job as job
    from featherweight.jobs import run_job as run_job
    from featherweight.log import setup_logging as setup_logging
    from featherweight.mcp import HttpMCPServer as HttpMCPServer
    from featherweight.mcp import StdioMCPServer as StdioMCPServer
    from featherweight.messages import to_agui as to_agui
    from featherweight.messages import to_langchain as to_langchain
    from featherweight.model_settings import Claude as Claude
    from featherweight.model_settings import Gemini as Gemini
    from featherweight.model_settings import Gemma as Gemma
    from featherweight.model_settings import ModelInfo as ModelInfo
    from featherweight.model_settings import ModelSettings as ModelSettings
    from featherweight.model_settings import OpenAI as OpenAI
    from featherweight.models import AgentBase as AgentBase
    from featherweight.models import AgentInput as AgentInput
    from featherweight.models import AgentOutput as AgentOutput
    from featherweight.models import AgentResult as AgentResult
    from featherweight.models import RunStatus as RunStatus
    from featherweight.observers import AttemptEnd as AttemptEnd
    from featherweight.observers import AttemptStart as AttemptStart
    from featherweight.observers import JobEnd as JobEnd
    from featherweight.observers import JobStart as JobStart
    from featherweight.observers import JobSubmitted as JobSubmitted
    from featherweight.observers import ModelCallEnd as ModelCallEnd
    from featherweight.observers import ModelCallStart as ModelCallStart
    from featherweight.observers import Observer as Observer
    from featherweight.observers import ReusedRun as ReusedRun
    from featherweight.observers import Scope as Scope
    from featherweight.observers import ToolCallEnd as ToolCallEnd
    from featherweight.observers import ToolCallStart as ToolCallStart
    from featherweight.otel import OTelObserver as OTelObserver
    from featherweight.postgres import PostgresDatabase as PostgresDatabase
    from featherweight.queue import Delivery as Delivery
    from featherweight.queue import JobMessage as JobMessage
    from featherweight.queue import JobQueue as JobQueue
    from featherweight.queue import SqliteQueue as SqliteQueue
    from featherweight.results import ResultKey as ResultKey
    from featherweight.results import ResultStore as ResultStore
    from featherweight.results import SqliteResultStore as SqliteResultStore
    from featherweight.runner import AgentRunner as AgentRunner
    from featherweight.skills import Skill as Skill
    from featherweight.sqlite import SqliteDatabase as SqliteDatabase
    from featherweight.streaming import ChatInput as ChatInput
    from featherweight.streaming import StreamingAgent as StreamingAgent
    from featherweight.streaming import StreamingRun as StreamingRun
    from featherweight.worker import JobWorker as JobWorker


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is not None:
        value = getattr(importlib.import_module(f"featherweight.{module}"), name)
    elif name in _SUBMODULES:
        value = importlib.import_module(f"featherweight.{_SUBMODULES[name]}")
    elif name in _MODULES:
        value = importlib.import_module(f"featherweight.{name}")
    else:
        raise AttributeError(f"module 'featherweight' has no attribute {name!r}")
    globals()[name] = value  # found once, then a plain attribute
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS, *_SUBMODULES, *_MODULES})
