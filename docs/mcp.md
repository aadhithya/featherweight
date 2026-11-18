# MCP tools

An agent can use the tools of [MCP](https://modelcontextprotocol.io) servers. The agent declares the servers; the runner
connects to them and gives the agent their tools next to its own. Defining agents is in [agents.md](agents.md). The
design and what was checked is in [specs/mcp-tools.md](specs/mcp-tools.md).

- [Declaring a server](#declaring-a-server)
- [Running an agent that uses MCP](#running-an-agent-that-uses-mcp)
- [What the model gets](#what-the-model-gets)
- [Failures](#failures)
- [Connections](#connections)
- [Logging](#logging)
- [Caveats](#caveats)
- [What is not supported](#what-is-not-supported)

## Declaring a server

```python
from micro.mcp import HttpMCPServer, StdioMCPServer


class Triager(AgentBase):
    ...
    mcp_servers: list[StdioMCPServer | HttpMCPServer] = [
        StdioMCPServer(
            name="github",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-github"),
            env_from=("GITHUB_TOKEN",),  # names of environment variables, never values
            tools=("search_issues", "get_issue"),  # only these; leave it out for all of them
        ),
        HttpMCPServer(name="docs", url="https://example.com/mcp", bearer_from="DOCS_TOKEN"),
    ]
```

Both are frozen Pydantic models, checked when they are built. Install the extra: `pip install 'micro[mcp]'`. The models
import without it; it is needed when a server is connected.

| Field | Default | |
|---|---|---|
| `name` | required | Lowercase letters and digits joined by single underscores, up to 20 characters. It is the prefix of the tool names: the tool `get_issue` of `github` is `github_get_issue`. Unique within the agent |
| `tools` | all | The server's own tool names that the agent may use. A name the server does not have fails the attempt, because it is usually a typo |
| `timeout_s` | 60 | For one call, and for starting the server |
| `max_result_chars` | 20,000 | The cap on the text of one result (see [below](#what-the-model-gets)) |
| `description` | empty | Not used yet. Kept for tool discovery |
| `command`, `args`, `env_from`, `cwd` | | A stdio server: the process to start, and the names of the environment variables to pass it |
| `url`, `bearer_from`, `headers_from` | | A streamable HTTP server. `bearer_from` is the name of the environment variable that holds a token, sent as `Authorization: Bearer <token>`. `headers_from` maps other header names to the name of the variable that holds the value |

**Secrets are names.** The definition holds the name of the variable. The value is read when the server is started, and
it is in no log line, run row, error or prompt hash. A variable that is not set (or is empty) fails the attempt and names
the variable.

A server that stdio starts gets a default environment (`PATH`, `HOME` and the like) plus the variables in `env_from`.

## Running an agent that uses MCP

The runner holds the connections, so it must be open:

```python
async with AgentRunner(db) as runner:
    result = await runner.run(Triager, TriageInput(...), job_id=job_id)
```

Running an agent with `mcp_servers` on a runner that is not open raises a `RuntimeError` and starts nothing. A runner
whose agents have no servers needs no `async with`. Without the block, call `await runner.aclose()` when you are done.

## What the model gets

The tools of the servers are bound to the model like the agent's own, with their schemas. An agent's own `tools`, MCP
tools and the tools the runner adds ([launching](launching-agents.md), [skills](skills.md)) are checked together before
the model sees any: two tools with one name fail the attempt, and so does a name Gemini would refuse. The run log's
`tool_names` lists them all.

| The tool returns | The model gets |
|---|---|
| Text | The text |
| An image, or a file that is part of the result | It, as the model's own image or file input |
| A link to a file | A line of text: `Resource link: <uri> (<type>)`. The link is not fetched |
| Structured data and no text | The data as JSON text |
| Audio, or anything the adapter cannot convert | A line of text saying so |

**The text is capped** at the server's `max_result_chars`, over all the text blocks of one result. Longer text is cut and
ends with `[truncated: 48,213 characters, showing the first 20,000]`. A result counts in the next model call's input
tokens, and an oversized one can exceed the context limit and fail the attempt. Images and files are not capped.

## Failures

Nothing an MCP server does raises into the agent's run.

| Case | What happens |
|---|---|
| The tool returns an error | The model gets the error text and carries on |
| A call exceeds `timeout_s` | The model gets `The call to 'x' timed out after 60s` |
| The server dies, or the connection drops, during a call | The model gets `MCP server 'x' is unavailable: ...`. The next attempt or run opens a new connection |
| Bad arguments | The server validates them, and its error text goes to the model |
| A missing variable, a command that does not exist, an HTTP server that answers 4xx (such as a wrong token: 401), an unknown tool in `tools`, two tools with one name | The attempt fails at once, **without retries**, and the message names the server or tool |
| The server fails to start for another reason, or does not start in time | An ordinary failed attempt, which is retried |

## Connections

- **Nothing connects when the runner is created.** A server starts the first time an attempt needs it, before the attempt
  takes a concurrency slot, so a slow start (`npx` downloading) holds none.
- **One connection per distinct server, shared.** Agents whose servers have equal connection settings (the command, args,
  cwd and variable names, or the URL and header names) share one process. Their names for the server, tool filters,
  timeouts and caps can differ. Calls from many runs share the connection at the same time.
- **Each connection has an owner task** that opens and closes it, because the MCP library requires one task to do both.
  `async with`, or `aclose()`, stops every server the runner started.
- **A launched agent** uses the servers it declares itself, through the same runner.

## Logging

Each call logs `mcp_call agent_run_id=... server=... tool=... status=ok|error|timeout|unavailable ms=...`, and starting and
stopping a server logs `mcp_open server=... transport=...` and `mcp_close server=...`. Arguments and results are not
logged. No new table or column: the tools of an attempt are in `tool_names`.

## Caveats

**Gemini's conversion of tool schemas is lenient and lossy.** It does not raise, but nested optional fields can come out
as required, a `oneOf` collapses to its first type, a dictionary with typed values becomes a bare object, and `format`,
`minimum` and `maximum` are dropped. The server still validates a call, and its error text reaches the model. If a tool
behaves oddly, look at the schema the model was given. This is the same kind of limit as the structured-output caveat in
[agents.md](agents.md#checking-the-output).

## What is not supported

- Finding tools (`search_tools`): an agent gets every tool its servers list, and `tools` is the only filter. This is
  [issue #3](https://github.com/aadhithya/micro/issues/3).
- SSE and websocket transports, OAuth for HTTP servers, and MCP resources, prompts and sampling.
- A refresh of a server's tool list while it runs. It is read when the connection is opened.
- Using a tool's annotations (`destructiveHint` and so on) as policy.
