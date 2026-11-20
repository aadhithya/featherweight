"""A tiny MCP server: who owns a service. The agent starts it as a subprocess and asks it questions."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("services", log_level="WARNING")  # the server is quiet: its output reaches your terminal

OWNERS = {
    "checkout": "payments team, on call: Ana",
    "search": "discovery team, on call: Ben",
    "batch": "data platform team, on call: Chen",
    "login": "identity team, on call: Dara",
}


@server.tool()
def owner_of(service: str) -> str:
    """Who owns a service, and who is on call."""
    return OWNERS.get(service.lower(), "unknown service")


if __name__ == "__main__":
    server.run()
