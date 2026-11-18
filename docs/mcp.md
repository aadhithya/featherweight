# MCP tools

An agent can use the tools of [MCP](https://modelcontextprotocol.io) servers. The agent declares the servers; the runner
connects to them and gives the agent their tools next to its own. Defining agents is in [agents.md](agents.md). The
design and what was checked is in [specs/mcp-tools.md](specs/mcp-tools.md).

- [Declaring a server](#declaring-a-server)
- [Running an agent that uses MCP](#running-an-agent-that-uses-mcp)
- [What the model gets](#what-the-model-gets)
- [Finding tools by search](#finding-tools-by-search)
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
| `discover` | `True` | `True`: the server's tools are found with `search_tools` and run with `call_tool`, and none is bound to the model. `False`: they are bound directly. See [below](#finding-tools-by-search) |
| `description` | empty | What the server is for. With `discover`, the model reads it to know what to search for |
| `command`, `args`, `env_from`, `cwd` | | A stdio server: the process to start, and the names of the environment variables to pass it |
| `url`, `bearer_from`, `headers_from` | | A streamable HTTP server. `bearer_from` is the name of the environment variable that holds a token, sent as `Authorization: Bearer <token>`. `headers_from` maps other header names to the name of the variable that holds the value |

**Secrets are names.** The definition holds the name of the variable. The value is read when the server is started, and
it is in no log line, run row, error or prompt hash. A variable that is not set (or is empty) fails the attempt and names
the variable.

A server that stdio starts gets a default environment (`PATH`, `HOME` and the like) plus the variables in `env_from`.

## Running an agent that uses MCP

Nothing special: run it like any agent.

```python
result = await AgentRunner(db).run(Triager, TriageInput(...), job_id=job_id)
```

A server is started when a run needs it and **stopped when the last run using it ends**, so nothing is left running.
Starting costs time (`npx` can take seconds), so when you run agents with the same servers more than once, keep them up
with `async with`:

```python
async with AgentRunner(db) as runner:  # the servers stay up between runs, and stop when the block ends
    await runner.run(Triager, ...)
    await runner.run(Triager, ...)
```

`await runner.aclose()` does the same as leaving the block. The block is optional and changes only how long servers live.

## What the model gets

The tools of a server with `discover=False` are bound to the model like the agent's own, with their schemas. (The default
is to [find them by search](#finding-tools-by-search) instead, and this section is about what a tool returns either way.) An agent's own `tools`, MCP
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

## Finding tools by search

**By default an MCP server's tools are not bound to the model.** A server can offer dozens, and binding all of them puts
every schema in every model call. The model gets two tools instead, and the prompt names the servers it can search:

```
You can find more tools by searching. These servers have tools you can search for:
- github: Issues, pull requests and code search
```

| Tool | |
|---|---|
| `search_tools(keywords)` | The best 5 matching tools, each with its name, description and input schema. Matched by BM25 over the tool's name, description and argument names |
| `call_tool(name, args_json)` | Runs a tool. `args_json` is a JSON string. It is checked against the tool's real schema first, and a call that does not fit returns the errors **and the schema**, so the model can fix it without searching again |

- **Search only when the tool is not in an earlier result.** Results stay in the conversation, and the prompt says so.
- **`call_tool` needs no earlier search.** The agent's `tools` filter is the boundary. A name that is not a tool of the agent
  gets a message to search for it.
- A discovered tool has the same timeout, size cap and failures-as-text as a bound one.
- **Opt out with `discover=False`** for a small server, where one native call beats a search and a call. One agent can bind
  one server and search another. The agent's own `tools` are always bound.
- The run log's `tool_names` shows `search_tools` and `call_tool`, not the tools behind them. Each search logs
  `tool_search ... keywords=... hits=...` and each call `tool_call_via_search ... status=ok|invalid`.

[specs/tool-discovery.md](specs/tool-discovery.md) has the design.

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

Two parts, with different jobs:

| | Is shared? | Does |
|---|---|---|
| **Connections** (`micro/mcp_connections.py`) | Yes: one per runner, across agents, runs and retries | Starts, shares and stops servers. Hands out a server's own tool list and its session. Knows nothing about an agent's names, filters, timeouts or caps |
| **The agent's tools** (`micro/mcp_tools.py`, added to the attempt's registry) | No: one per attempt of one agent | Applies the agent's declaration to the server's tools: the `tools` filter, the `<server>_` prefix, the timeout and cap, and turning failures into text |

What can differ between two agents that share a server is on the agent's side. What cannot is on the connection's side.

- **Nothing connects when the runner is created.** A server starts when a run first needs it, before its attempt takes a
  concurrency slot, so a slow start (`npx` downloading) holds none.
- **A run holds a lease** on its servers, from its first attempt to its last, so a retry does not start a server again. A
  server stops when its last lease is released, or, inside `async with`, when the block ends.
- **One connection per distinct server.** Agents whose servers have equal connection settings (the command, args, cwd and
  variable names, or the URL and header names) share one process, also while they run at the same time.
- **Each connection has an owner task** that opens and closes it, because the MCP library requires one task to do both.
- **A launched agent** uses the servers it declares itself, through the same runner, and holds its own lease.

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

- Finding tools of the agent's own `tools`, skills or agents. Only MCP tools are found by search.
- SSE and websocket transports, OAuth for HTTP servers, and MCP resources, prompts and sampling.
- A refresh of a server's tool list while it runs. It is read when the connection is opened.
- Using a tool's annotations (`destructiveHint` and so on) as policy.
