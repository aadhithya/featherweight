"""How an agent declares the MCP servers it uses. These are plain, frozen Pydantic models: they only describe a
server. Connecting to it is the runner's job (see `micro/mcp_runtime.py`), so this module needs no MCP install."""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Lowercase words joined by single underscores. The name is the prefix of the server's tool names (`github_get_issue`).
_SERVER_NAME = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
MAX_SERVER_NAME = 20

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RESULT_CHARS = 20_000


class MCPServer(BaseModel):
    """What every MCP server declaration has."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""  # reserved for tool discovery
    # Which of the server's tools the agent gets, by the names the server uses. None: all of them.
    tools: tuple[str, ...] | None = None
    timeout_s: Annotated[float, Field(gt=0)] = DEFAULT_TIMEOUT_S  # per call, and for starting the server
    max_result_chars: Annotated[int, Field(ge=1)] = DEFAULT_MAX_RESULT_CHARS  # cap on the text of one result

    @field_validator("name")
    @classmethod
    def _valid_name(cls, name: str) -> str:
        if not _SERVER_NAME.match(name) or len(name) > MAX_SERVER_NAME:
            raise ValueError(
                f"MCP server name {name!r} must be lowercase letters and digits joined by single underscores, "
                f"at most {MAX_SERVER_NAME} characters"
            )
        return name

    @field_validator("tools")
    @classmethod
    def _valid_tools(cls, tools: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if tools is None:
            return None
        if not all(tools):
            raise ValueError("a tool name in `tools` is empty")
        if len(set(tools)) != len(tools):
            raise ValueError(f"`tools` lists a tool twice: {list(tools)}")
        return tools

    def connection_key(self) -> tuple:
        """What identifies the connection: two servers with an equal key share one."""
        raise NotImplementedError


class StdioMCPServer(MCPServer):
    """A server the runner starts as a subprocess and talks to over stdin and stdout."""

    command: str
    args: tuple[str, ...] = ()
    env_from: tuple[str, ...] = ()  # names of environment variables to hand to the process, never their values
    cwd: str | None = None

    @field_validator("command")
    @classmethod
    def _command_given(cls, command: str) -> str:
        if not command.strip():
            raise ValueError("`command` is empty")
        return command

    @field_validator("env_from")
    @classmethod
    def _env_names(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if not all(names):
            raise ValueError("a name in `env_from` is empty")
        return names

    def connection_key(self) -> tuple:
        return ("stdio", self.command, self.args, tuple(sorted(set(self.env_from))), self.cwd)


class HttpMCPServer(MCPServer):
    """A server reached over streamable HTTP."""

    url: str
    # The name of an environment variable that holds a token. It is sent as `Authorization: Bearer <token>`.
    bearer_from: str | None = None
    headers_from: dict[str, str] = Field(default_factory=dict)  # header name -> name of an environment variable

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"`url` must start with http:// or https://, got {url!r}")
        return url

    @field_validator("bearer_from")
    @classmethod
    def _bearer_name(cls, name: str | None) -> str | None:
        if name is not None and not name:
            raise ValueError("`bearer_from` is empty")
        return name

    @model_validator(mode="after")
    def _one_authorization(self):
        if self.bearer_from is not None and any(header.lower() == "authorization" for header in self.headers_from):
            raise ValueError("`bearer_from` sets the Authorization header, and `headers_from` sets it too")
        return self

    def connection_key(self) -> tuple:
        return ("http", self.url, self.bearer_from, tuple(sorted(self.headers_from.items())))
