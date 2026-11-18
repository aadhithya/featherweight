"""The agent's side of MCP: how one agent sees the tools of the servers it declares. Which tools it gets, what they are
called, how long a call may take, what the model is shown, and how a failure reads all follow from the agent's own
declaration, so none of it is in `mcp_connections.py`, which is shared between agents."""

import asyncio
import json
import logging
import time
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from micro.context import CURRENT_AGENT_RUN_ID
from micro.errors import SetupError
from micro.mcp import MCPServer
from micro.mcp_connections import Lease, require_adapter

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 500


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


async def mcp_tools(server: MCPServer, lease: Lease) -> list[BaseTool]:
    """The tools of `server` that its agent may use, named `<server>_<tool>`, ready to be given to the model."""
    require_adapter()
    from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

    available = await lease.list_tools(server)
    if server.tools is not None:
        names = {tool.name for tool in available}
        missing = [name for name in server.tools if name not in names]
        if missing:
            offered = ", ".join(sorted(names)) or "none"
            raise SetupError(f"MCP server '{server.name}' has no tool {', '.join(missing)}. It has: {offered}")
        available = [tool for tool in available if tool.name in server.tools]
    session = await lease.session(server)
    return [
        _wrap(
            convert_mcp_tool_to_langchain_tool(
                session, tool, server_name=server.name, tool_name_prefix=True, handle_tool_errors=True
            ),
            server,
            lease,
        )
        for tool in available
    ]


def _wrap(tool: BaseTool, server: MCPServer, lease: Lease) -> BaseTool:
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
            lease.drop(server)
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
