"""The tools one attempt of an agent may use, checked together before the model sees any of them."""

import re
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from micro.errors import SetupError

# What Gemini accepts as a function name: it starts with a letter or an underscore, and has at most 64 characters.
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\-]{0,63}$")


@dataclass(frozen=True)
class RegistryEntry:
    tool: BaseTool
    source: str  # "local", "mcp" or "runner"


class ToolRegistry:
    """Every tool of one attempt, with where it came from: the agent's own tools, the tools of its MCP servers, and
    the ones the runner adds. Adding a tool whose name is taken, or one the model could not be given, raises
    `SetupError`. Today every tool is bound to the model directly."""

    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}

    def add(self, tool: BaseTool, source: str) -> None:
        if not _TOOL_NAME.match(tool.name):
            raise SetupError(
                f"The tool '{tool.name}' ({source}) cannot be given to the model: a tool name starts with a letter or "
                "an underscore, uses letters, digits and _ . : -, and has at most 64 characters"
            )
        taken = self._entries.get(tool.name)
        if taken is not None:
            raise SetupError(f"Two tools are called '{tool.name}' ({taken.source} and {source})")
        self._entries[tool.name] = RegistryEntry(tool, source)

    @property
    def tools(self) -> list[BaseTool]:
        return [entry.tool for entry in self._entries.values()]

    @property
    def names(self) -> list[str]:
        return list(self._entries)
