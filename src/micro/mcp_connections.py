"""The runner's connections to MCP servers: they start servers, share them, and stop them. Nothing here knows what an
agent calls a server, which of its tools it may use, or how long it waits: that is the agent's side (`mcp_tools.py`).

A server is held by an owner task that opens the session, publishes it, and waits to be told to stop. The session is
opened and closed by that one task, which the MCP library requires, and any task can call through it meanwhile.

A run holds a `Lease` on the servers it uses. A server is started when the first lease needs it, and stopped when the
last lease is released, unless the runner is open (`keep_open`), when it stays until `aclose`.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from micro.errors import RETRYABLE_4XX, SetupError
from micro.mcp import HttpMCPServer, MCPServer, StdioMCPServer

logger = logging.getLogger(__name__)


def require_adapter() -> None:
    """MCP needs the `mcp` extra; say so when it is missing."""
    try:
        import langchain_mcp_adapters  # noqa: F401
    except ImportError as exc:
        raise SetupError("MCP servers need an extra install: pip install 'micro[mcp]'") from exc


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


@dataclass(eq=False)
class _Entry:
    """One open connection."""

    key: tuple
    name: str  # what the server was called when this connection was opened
    ready: asyncio.Future[None] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    session: Any = None
    users: int = 0  # the leases that hold it
    dead: bool = False
    raw_tools: list[Any] | None = None  # the server's own tool list, read once
    tools_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MCPConnections:
    """The open MCP connections of one runner: one per distinct server, shared by every lease on an equal server."""

    def __init__(self, *, keep_open: bool = False) -> None:
        self.keep_open = keep_open  # when True, a server with no lease left stays open until `aclose`
        self._entries: dict[tuple, _Entry] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    async def aclose(self) -> None:
        """Stop every server, whatever holds it. Safe to call twice. A later lease starts them again."""
        self.keep_open = False
        for entry in list(self._entries.values()):
            await self._stop(entry)

    async def _acquire(self, server: MCPServer) -> _Entry:
        key = server.connection_key()
        async with self._locks.setdefault(key, asyncio.Lock()):  # concurrent first uses open one connection
            entry = self._entries.get(key)
            if entry is None or entry.dead:
                entry = await self._open(server, key)
            entry.users += 1
            return entry

    async def _release(self, entry: _Entry) -> None:
        async with self._locks.setdefault(entry.key, asyncio.Lock()):
            entry.users -= 1
            if entry.users <= 0 and (not self.keep_open or entry.dead):
                await self._stop(entry)

    async def _stop(self, entry: _Entry) -> None:
        entry.stop.set()
        if entry.task is not None:
            await asyncio.gather(entry.task, return_exceptions=True)
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]

    def _drop(self, entry: _Entry) -> None:
        """The connection failed: forget it, so that the next lease opens a new one, and let its owner end."""
        entry.dead = True
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]
        entry.stop.set()

    async def _open(self, server: MCPServer, key: tuple) -> _Entry:
        require_adapter()
        from langchain_mcp_adapters.client import MultiServerMCPClient

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
            import httpx

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

    async def _own(self, entry: _Entry, client: Any) -> None:
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


class Lease:
    """What one run holds on the servers it uses. It gives the run a server's raw tools and its session, starting the
    server if nobody has, and `release` lets go of all of them. A server whose connection died is replaced the next
    time it is asked for."""

    def __init__(self, connections: Any):
        self._get_connections = connections  # a function, so that nothing is created for a run without servers
        self._held: dict[tuple, _Entry] = {}

    async def _entry(self, server: MCPServer) -> _Entry:
        key = server.connection_key()
        held = self._held.get(key)
        if held is not None and not held.dead:
            return held
        connections = self._get_connections()
        entry = await connections._acquire(server)
        self._held[key] = entry
        if held is not None:  # replaced: let go of the dead one
            await connections._release(held)
        return entry

    async def list_tools(self, server: MCPServer) -> list[Any]:
        """The server's own tools (`mcp.types.Tool`), as it lists them. Read once for a connection."""
        entry = await self._entry(server)
        async with entry.tools_lock:
            if entry.raw_tools is None:
                try:
                    entry.raw_tools = await _list_all_tools(entry.session)
                except Exception:
                    self.drop(server)  # a connection that cannot even list its tools is no use
                    raise
            return entry.raw_tools

    async def session(self, server: MCPServer) -> Any:
        return (await self._entry(server)).session

    def drop(self, server: MCPServer) -> None:
        """The run found the connection broken: the next lease on this server opens a new one."""
        entry = self._held.get(server.connection_key())
        if entry is not None:
            self._get_connections()._drop(entry)

    async def release(self) -> None:
        held, self._held = list(self._held.values()), {}
        for entry in held:
            await self._get_connections()._release(entry)


async def _list_all_tools(session: Any) -> list[Any]:
    tools: list[Any] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools
