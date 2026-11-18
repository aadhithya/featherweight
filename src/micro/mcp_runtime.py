"""Connections to MCP servers, and their tools in the shape the model is given.

Needs the `mcp` extra (`pip install micro[mcp]`); the runner imports this module only when an agent has servers.

Each server is held by an owner task that opens the session, publishes it, and waits to be told to stop. The session is
opened and closed by that one task, which the MCP library requires, and any task can call through it meanwhile.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from micro.context import CURRENT_AGENT_RUN_ID
from micro.errors import RETRYABLE_4XX, SetupError
from micro.mcp import HttpMCPServer, MCPServer, StdioMCPServer

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500


def _leaf(exc: BaseException) -> BaseException:
    """The error inside the exception groups that anyio wraps around failures in a session."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _env_value(server: MCPServer, variable: str) -> str:
    value = os.environ.get(variable)
    if not value:
        raise SetupError(f"MCP server '{server.name}' needs the environment variable {variable}, which is not set")
    return value


def connection_config(server: MCPServer) -> dict[str, Any]:
    """The adapter's connection settings for `server`, with the secrets read from the environment."""
    if isinstance(server, StdioMCPServer):
        config: dict[str, Any] = {"transport": "stdio", "command": server.command, "args": list(server.args)}
        if server.env_from:
            config["env"] = {variable: _env_value(server, variable) for variable in server.env_from}
        if server.cwd is not None:
            config["cwd"] = server.cwd
        return config
    if isinstance(server, HttpMCPServer):
        config = {"transport": "streamable_http", "url": server.url}
        headers = {header: _env_value(server, variable) for header, variable in server.headers_from.items()}
        if server.bearer_from is not None:
            headers["Authorization"] = f"Bearer {_env_value(server, server.bearer_from)}"
        if headers:
            config["headers"] = headers
        return config
    raise TypeError(f"Unknown MCP server type {type(server).__name__}")


def present(content: Any, artifact: Any, max_chars: int) -> list[dict[str, Any]]:
    """What the model is given for a tool result.

    Text, images and embedded files pass through. A link to a file becomes a line of text: left as a file block, the
    Gemini client would fetch the address itself. A result with no content but with structured content is shown as
    JSON. The text is cut at `max_chars`, over all its blocks together.
    """
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    for block in content or []:
        if not isinstance(block, dict):
            blocks.append({"type": "text", "text": str(block)})
        elif block.get("type") in ("image", "file") and block.get("url") and not block.get("base64"):
            mime = block.get("mime_type") or "unknown type"
            blocks.append({"type": "text", "text": f"Resource link: {block['url']} ({mime})"})
        else:
            blocks.append(block)

    if not any(block.get("text") or block.get("type") != "text" for block in blocks):
        structured = artifact.get("structured_content") if isinstance(artifact, dict) else None
        if structured is not None:
            blocks = [{"type": "text", "text": json.dumps(structured, indent=2)}]
        else:
            blocks = [{"type": "text", "text": "[the tool returned no content]"}]

    total = sum(len(block.get("text", "")) for block in blocks if block.get("type") == "text")
    if total > max_chars:
        left = max_chars
        cut: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("type") != "text":
                cut.append(block)
            elif left > 0:
                text = block["text"][:left]
                left -= len(text)
                cut.append({**block, "text": text})
        cut.append({"type": "text", "text": f"[truncated: {total:,} characters, showing the first {max_chars:,}]"})
        blocks = cut
    return blocks


@dataclass(eq=False)
class _Entry:
    """One open connection."""

    key: tuple
    name: str  # what the server was called when this connection was opened
    ready: asyncio.Future[None] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    session: Any = None
    dead: bool = False
    loaded: dict[tuple, list[BaseTool]] = field(default_factory=dict)
    load_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MCPConnections:
    """The runner's open MCP connections: one per distinct server, opened on first use and shared by every agent that
    declares an equal server. `aclose` ends them all."""

    def __init__(self) -> None:
        self._entries: dict[tuple, _Entry] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._closed = False

    async def tools_for(self, server: MCPServer) -> list[BaseTool]:
        """The tools of `server` that its agent may use, named `<server>_<tool>`, ready to be given to the model."""
        entry = await self._entry(server)
        try:
            tools = await self._load(entry, server)
        except Exception:
            self._drop(entry)  # the connection is no use if it cannot even list its tools
            raise
        if server.tools is None:
            return tools
        wanted = [f"{server.name}_{name}" for name in server.tools]
        known = {tool.name for tool in tools}
        missing = [name for name in server.tools if f"{server.name}_{name}" not in known]
        if missing:
            offered = ", ".join(sorted(name.removeprefix(f"{server.name}_") for name in known)) or "none"
            raise SetupError(f"MCP server '{server.name}' has no tool {', '.join(missing)}. It has: {offered}")
        return [tool for tool in tools if tool.name in wanted]

    async def aclose(self) -> None:
        """Stop every server this object started. Safe to call twice."""
        self._closed = True
        entries = list(self._entries.values())
        for entry in entries:
            entry.stop.set()
        tasks = [entry.task for entry in entries if entry.task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _entry(self, server: MCPServer) -> _Entry:
        if self._closed:
            raise RuntimeError("These MCP connections are closed")
        key = server.connection_key()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # concurrent first uses open one connection
            entry = self._entries.get(key)
            if entry is not None and not entry.dead:
                return entry
            return await self._open(server, key)

    async def _open(self, server: MCPServer, key: tuple) -> _Entry:
        client = MultiServerMCPClient({server.name: connection_config(server)})  # ty: ignore[invalid-argument-type]  # a plain dict is the connection settings
        entry = _Entry(key=key, name=server.name)
        entry.task = asyncio.create_task(self._own(entry, client))
        self._entries[key] = entry
        try:
            await asyncio.wait_for(entry.ready, server.timeout_s)
        except BaseException as exc:
            entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)
            if self._entries.get(key) is entry:
                del self._entries[key]
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, TimeoutError):
                raise RuntimeError(f"MCP server '{server.name}' did not start within {server.timeout_s:g}s") from None
            error = _leaf(exc)
            if isinstance(error, OSError) and isinstance(server, StdioMCPServer):
                raise SetupError(f"MCP server '{server.name}' could not be started: {error}") from error
            if isinstance(error, httpx.HTTPStatusError) and isinstance(server, HttpMCPServer):
                status = error.response.status_code
                if (
                    400 <= status < 500 and status not in RETRYABLE_4XX
                ):  # a wrong token or URL: another try would fail too
                    raise SetupError(
                        f"MCP server '{server.name}' refused the connection: {status} {error.response.reason_phrase} "
                        f"({server.url})"
                    ) from error
            raise error from None
        logger.info(
            "mcp_open server=%s transport=%s", server.name, "stdio" if isinstance(server, StdioMCPServer) else "http"
        )
        return entry

    async def _own(self, entry: _Entry, client: MultiServerMCPClient) -> None:
        """The owner task of one connection: opens the session, then holds it until told to stop."""
        try:
            async with client.session(entry.name) as session:
                entry.session = session
                if not entry.ready.done():
                    entry.ready.set_result(None)
                await entry.stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not entry.ready.done():
                entry.ready.set_exception(_leaf(exc))
        finally:
            entry.dead = True
            if self._entries.get(entry.key) is entry:
                del self._entries[entry.key]
            logger.info("mcp_close server=%s", entry.name)

    def _drop(self, entry: _Entry) -> None:
        """The connection failed: forget it, so that the next use opens a new one, and let its owner end."""
        entry.dead = True
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]
        entry.stop.set()

    async def _load(self, entry: _Entry, server: MCPServer) -> list[BaseTool]:
        """All the tools of the connection, wrapped, for this server's settings. Loaded once for each."""
        cache_key = (server.name, server.timeout_s, server.max_result_chars)
        async with entry.load_lock:
            if cache_key not in entry.loaded:
                raw = await load_mcp_tools(
                    entry.session, server_name=server.name, tool_name_prefix=True, handle_tool_errors=True
                )
                entry.loaded[cache_key] = [self._wrap(tool, server, entry) for tool in raw]
            return entry.loaded[cache_key]

    def _wrap(self, tool: BaseTool, server: MCPServer, entry: _Entry) -> BaseTool:
        """The same tool, but nothing raises out of it: a failure is text for the model. Each call is logged."""

        async def call(**arguments: Any) -> list[dict[str, Any]]:
            started = time.perf_counter()
            status = "ok"
            try:
                message = await asyncio.wait_for(
                    tool.ainvoke({"type": "tool_call", "name": tool.name, "args": arguments, "id": "mcp"}),
                    server.timeout_s,
                )
                if getattr(message, "status", "success") == "error":
                    status = "error"
                return present(message.content, getattr(message, "artifact", None), server.max_result_chars)
            except TimeoutError:
                status = "timeout"
                text = f"The call to '{tool.name}' timed out after {server.timeout_s:g}s"
            except (NotImplementedError, ValueError) as exc:  # a result the adapter cannot convert, such as audio
                status = "error"
                text = f"The tool '{tool.name}' returned something that cannot be shown: {exc}"[:MAX_ERROR_CHARS]
            except Exception as exc:  # the connection is gone
                status = "unavailable"
                self._drop(entry)
                text = f"MCP server '{server.name}' is unavailable: {type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            finally:
                logger.info(
                    "mcp_call agent_run_id=%s server=%s tool=%s status=%s ms=%.0f",
                    CURRENT_AGENT_RUN_ID.get(),
                    server.name,
                    tool.name,
                    status,
                    (time.perf_counter() - started) * 1000,
                )
            return [{"type": "text", "text": text}]

        return StructuredTool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema or {"type": "object", "properties": {}},
            coroutine=call,
            metadata=tool.metadata,
        )
