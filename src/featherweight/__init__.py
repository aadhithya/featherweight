"""Featherweight: an opinionated agent harness. Pydantic is the spec.

    import featherweight as ftw

    class Summariser(ftw.AgentBase): ...
    runner = ftw.AgentRunner(db)

The public names are available on the package. Each is loaded when it is first used, so that `import featherweight` stays
light and imports no model provider. The modules are there too (`ftw.model_settings`, `ftw.adapters`, ...), and the two adapters are `ftw.agui` and `ftw.vercel`.
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
    "default_db_path": "db",
    "run_job": "jobs",
    "JobOutcome": "jobs",
    # what an agent can have, and what a tool can use
    "Skill": "skills",
    "StdioMCPServer": "mcp",
    "HttpMCPServer": "mcp",
    "RunStore": "store",
    "RunContext": "context",
    "SetupError": "errors",
    "BadRequest": "adapters.errors",
    "setup_logging": "log",
}
_SUBMODULES = {"agui": "adapters.agui", "vercel": "adapters.vercel"}  # the two adapters, as `ftw.agui` and `ftw.vercel`
_MODULES = (
    "adapters",
    "context",
    "db",
    "errors",
    "jobs",
    "log",
    "mcp",
    "messages",
    "model_settings",
    "models",
    "registry",
    "runner",
    "skills",
    "store",
    "streaming",
    "tools",
)

__all__ = ["__version__", *_EXPORTS, *_SUBMODULES]

if TYPE_CHECKING:  # for editors and type checkers: the same names, imported
    from featherweight import adapters as adapters
    from featherweight.adapters import agui as agui
    from featherweight.adapters import vercel as vercel
    from featherweight.adapters.errors import BadRequest as BadRequest
    from featherweight.context import RunContext as RunContext
    from featherweight.db import Database as Database
    from featherweight.db import default_db_path as default_db_path
    from featherweight.errors import SetupError as SetupError
    from featherweight.jobs import JobOutcome as JobOutcome
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
    from featherweight.runner import AgentRunner as AgentRunner
    from featherweight.skills import Skill as Skill
    from featherweight.store import RunStore as RunStore
    from featherweight.streaming import ChatInput as ChatInput
    from featherweight.streaming import StreamingAgent as StreamingAgent
    from featherweight.streaming import StreamingRun as StreamingRun


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
