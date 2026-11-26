"""Helpers for the tests of MCP tools and tool discovery: a fake server, agents that use it, and what the model was shown."""

import sys
from pathlib import Path

from fakes import Echo, EchoInput, structured_reply
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from featherweight.mcp import HttpMCPServer, StdioMCPServer
from featherweight.model_settings import ModelSettings

SERVER_SCRIPT = str(Path(__file__).parent / "mcp_fake_server.py")


def fake(name="fake", **kwargs) -> StdioMCPServer:
    """The fake server. Its tools are bound directly unless the test asks for discovery."""
    kwargs.setdefault("discover", False)
    return StdioMCPServer(name=name, command=sys.executable, args=(SERVER_SCRIPT,), **kwargs)


def worker(*servers, tools=(), name="worker", model=None):
    """An agent class with the given servers and local tools, and optionally its own model settings."""
    server_list, tool_list, agent_name, settings = list(servers), list(tools), name, model or Echo().model

    class Worker(Echo):
        name: str = agent_name
        tools: list[BaseTool] = tool_list
        mcp_servers: list[StdioMCPServer | HttpMCPServer] = server_list
        model: ModelSettings = settings

    return Worker


def done():
    return structured_reply("EchoOutput", {"echoed": "ok"})


def shown(model) -> list[list]:
    """The content of every tool result the model was shown, in order, one list of blocks each."""
    seen: dict[str, list] = {}
    for messages in model.seen:
        for message in messages:
            if isinstance(message, ToolMessage):
                content = message.content
                seen[message.tool_call_id] = (
                    content if isinstance(content, list) else [{"type": "text", "text": content}]
                )
    return list(seen.values())


def texts(model) -> list[str]:
    return ["".join(block.get("text", "") for block in blocks) for blocks in shown(model)]


INPUT = EchoInput(text="go")


def use_real_stderr(monkeypatch) -> None:
    """For code that runs as a script would, under `capsys`. The MCP library binds the server's error stream to
    `sys.stderr` as it is when first imported, which under pytest can be a capture file that is closed by now, or one with no
    file number. A reader's terminal is the real one."""
    from mcp.client.stdio import stdio_client

    monkeypatch.setattr(stdio_client.__wrapped__, "__defaults__", (sys.__stderr__,))
