"""The tools one attempt of an agent may use, checked together before the model sees any of them."""

import re
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from featherweight.errors import SetupError
from featherweight.mcp import MCPServer
from featherweight.mcp_connections import Lease
from featherweight.search import Bm25

# What Gemini accepts as a function name: it starts with a letter or an underscore, and has at most 64 characters.
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\-]{0,63}$")

DIRECT = "direct"  # bound to the model
DISCOVERABLE = "discoverable"  # found with `search_tools` and run with `call_tool`


@dataclass(frozen=True)
class RegistryEntry:
    tool: BaseTool
    source: str  # "local", "mcp" or "runner"
    mode: str = DIRECT
    server: str = ""  # the agent's name for the MCP server it came from


def input_schema(tool: BaseTool) -> dict[str, Any]:
    """The JSON schema of a tool's input: the one an MCP server published, or the one langchain builds."""
    schema = tool.args_schema
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    return {"type": "object", "properties": {}}


class ToolRegistry:
    """Every tool of one attempt, with where it came from and how the model gets it: the agent's own tools, the tools of
    its MCP servers, and the ones the runner adds. Adding a tool whose name is taken, or one the model could not be
    given, raises `SetupError`. A direct tool is bound to the model; a discoverable one is found with a keyword search."""

    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        self._index: Bm25 | None = None  # over the discoverable tools; built on the first search

    def add(self, tool: BaseTool, source: str, *, mode: str = DIRECT, server: str = "") -> None:
        if not _TOOL_NAME.match(tool.name):
            raise SetupError(
                f"The tool '{tool.name}' ({source}) cannot be given to the model: a tool name starts with a letter or "
                "an underscore, uses letters, digits and _ . : -, and has at most 64 characters"
            )
        taken = self._entries.get(tool.name)
        if taken is not None:
            raise SetupError(f"Two tools are called '{tool.name}' ({taken.source} and {source})")
        self._entries[tool.name] = RegistryEntry(tool, source, mode, server)
        self._index = None

    async def add_mcp(self, server: MCPServer, lease: Lease) -> None:
        """Add the tools of one of the agent's MCP servers, as this agent's declaration says: the allow-list, the
        `<server>_` prefix, the timeout, the size cap, and whether they are found by search. The connection is shared
        and knows none of that."""
        from featherweight.mcp_tools import mcp_tools

        mode = DISCOVERABLE if server.discover else DIRECT
        for tool in await mcp_tools(server, lease):
            self.add(tool, "mcp", mode=mode, server=server.name)

    @property
    def tools(self) -> list[BaseTool]:
        """The tools bound to the model."""
        return [entry.tool for entry in self._entries.values() if entry.mode == DIRECT]

    @property
    def names(self) -> list[str]:
        """The names of the bound tools."""
        return [name for name, entry in self._entries.items() if entry.mode == DIRECT]

    @property
    def discoverable(self) -> list[RegistryEntry]:
        return [entry for entry in self._entries.values() if entry.mode == DISCOVERABLE]

    def entry(self, name: str) -> RegistryEntry | None:
        return self._entries.get(name)

    def search(self, keywords: list[str], k: int = 5) -> list[RegistryEntry]:
        """The discoverable tools that match the keywords, best first. A tool is indexed by its name (twice, because it is
        the strongest signal and carries the server's name), its description and the names of its arguments."""
        entries = self.discoverable
        if self._index is None:
            self._index = Bm25([_document(entry) for entry in entries])
        return [entries[index] for index, _ in self._index.search(" ".join(keywords), k)]


def _document(entry: RegistryEntry) -> str:
    tool = entry.tool
    arguments = " ".join(input_schema(tool).get("properties", {}))
    return f"{tool.name} {tool.name} {tool.description} {arguments}"
