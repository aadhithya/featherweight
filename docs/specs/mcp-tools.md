# Spec: MCP tools

Status: implemented. This is the design for [issue #1](https://github.com/aadhithya/featherweight/issues/1); how to use it is in
[mcp.md](../mcp.md). It was checked against `langchain-mcp-adapters` 0.3.2 (see [Findings](#findings-from-the-adapter)).

- [Goal and scope](#goal-and-scope)
- [Findings from the adapter](#findings-from-the-adapter)
- [Declaration](#declaration)
- [The tool registry](#the-tool-registry)
- [Connections and lifecycle](#connections-and-lifecycle)
- [Failures are values](#failures-are-values)
- [What reaches the model](#what-reaches-the-model)
- [Logging](#logging)
- [Not in this version](#not-in-this-version)
- [Tests](#tests)
- [Decisions](#decisions)

## Goal and scope

An agent declares the MCP servers it uses, as typed, frozen Pydantic models. Their tools are added to the agent like any
other tool: allow-listed per agent, bound to the model, traced, and with failures returned as values.

This spec is how MCP tools are connected and bound. Finding them by search (`discover`, `search_tools`, `call_tool`) was
added after, and is in [tool-discovery.md](tool-discovery.md): by default a server's tools are discoverable, and the sections
below about binding apply to a server with `discover=False`.

## Findings from the adapter

Checked with a fake stdio server, and once with a real Gemini call through `create_agent`.

**It fits.**
- Tools come back as langchain `StructuredTool`s, so they go into the registry like any other tool.
- A session passed to `load_mcp_tools(session)` is reused. Five concurrent calls ran in 0.51s for a 0.5s tool, all in
  one subprocess.
- `tool_name_prefix=True` prefixes tool names with the server name (`server_tool`).
- `env` values reach a stdio server, and `HOME` and `PATH` are still inherited.
- An MCP error result (`isError`) comes back as error text the model can read (`handle_tool_errors=True`).
- Tool annotations (`readOnlyHint` and so on) arrive in `tool.metadata`. `tool_interceptors` wrap every call and get the
  server name.
- Gemini accepted the tools, including a nested and nullable schema, and called them correctly.
- `mcp` brings `jsonschema`, which `call_tool` ([tool discovery](tool-discovery.md)) uses. About 50 packages in all, so it is an optional extra.

**It does not fit, and what we do.**
1. **By default each tool call starts a new session**, which for stdio is a new subprocess. We hold the sessions
   ourselves and pass them in.
2. **A session must be opened and closed by the same task.** Opening in one task and closing from another raised
   `Attempted to exit cancel scope in a different task`. An owner task per server, which opens the session and waits for
   a stop signal, works.
3. **A server that dies raises** (`McpError: Connection closed`, or `FileNotFoundError` for a missing command), and the
   run dies with it. The adapter handles only `isError` results. We wrap every MCP tool.

**Gemini's schema conversion is lenient and lossy.** It never raised, but: nested optional fields became required; a
`oneOf` collapsed to its first type; a dict with typed values became a bare object; `format`, `minimum` and `maximum`
were dropped. The server still validates the call and its error text reaches the model, so most of it is recoverable.
Document it next to the existing structured-output caveat.

## Declaration

```python
class MCPServer(BaseModel, frozen=True):
    name: str  # lowercase letters, digits and single underscores; the tool prefix
    description: str = ""  # what the server is for; the model reads it, to know what to search for
    discover: bool = True  # False: bind the tools directly (see tool-discovery.md)
    tools: tuple[str, ...] | None = None  # None: every tool of the server. A tuple: only those
    timeout_s: float = 60  # per call
    max_result_chars: int = 20_000  # cap on the text of one result; at least 1


class StdioMCPServer(MCPServer):
    command: str
    args: tuple[str, ...] = ()
    env_from: tuple[str, ...] = ()  # names of environment variables, never values
    cwd: str | None = None


class HttpMCPServer(MCPServer):  # streamable HTTP
    url: str
    bearer_from: str | None = None  # name of a variable; sent as `Authorization: Bearer <value>`
    headers_from: dict[str, str] = {}  # header name -> name of an environment variable


class AgentBase:
    mcp_servers: list[StdioMCPServer | HttpMCPServer] = []
```

Checked when the agent is built (a programming error that raises):
- server names are unique within the agent;
- a name is valid, and short enough that `name_tool` fits in 64 characters for typical tools;
- an allow-list has no empty or duplicate entries;
- `timeout_s` is above 0 and `max_result_chars` is at least 1.

**Secrets are names.** The agent definition holds environment variable names. They are resolved when the connection
opens. A missing variable is a setup failure that names the variable. Values never appear in a log, a run row or a prompt
hash.

**The models import without the extra.** Only the runner imports `mcp` and the adapter, lazily, and a missing install
raises a message that says `pip install featherweight[mcp]`.

## The tool registry

An internal class, built by the runner for each attempt. It holds every tool the attempt may use, with its source
(`local`, `mcp:<server>` or `runner`) and its mode (`direct` or `discoverable`; see [tool-discovery.md](tool-discovery.md)).

- **Naming.** MCP tools are named by the adapter: `load_mcp_tools(session, server_name=<the agent's name for the
  server>, tool_name_prefix=True)` gives `server_tool`. The registry does not rename tools. It loads once per connection
  and agent-side server name, and caches the result.
- **Collisions.** A name used twice, across local tools, MCP tools and the injected `launch_agent`, `get_agent_schema`
  and `get_skill`, is a setup failure. (A single underscore can clash: server `a_b` with tool `c`, and server `a` with
  tool `b_c`.) A local tool against an injected tool is caught when the agent is built.
- **Filtering.** `tools` on a server keeps only the listed tools. A listed name the server does not have is a setup
  failure, because it is usually a typo.
- **Binding.** The direct entries are what the model is bound to. The `attempt_started` log line lists them (`tools=`); the run log keeps `tools_hash`.

## Separation of concerns

Two parts, with different lifetimes:

| | Scope | Owns | Does not know |
|---|---|---|---|
| **`MCPConnections`** (`mcp_connections.py`) | One per runner, shared by agents, runs and retries | The server processes and sessions | An agent's names for servers, filters, timeouts, caps, modes |
| **`ToolRegistry`** (`registry.py`, with `mcp_tools.py`) | One per attempt of one agent | Nothing live: the tools it was given | Whether a server is shared, or when it stops |

Rule: what can differ between two agents that share a server is on the registry side; what cannot is on the connection
side. The connection key holds only connection settings, so `name`, `tools`, `timeout_s`, `max_result_chars` (and later
`discover`) never reach it.

```python
lease = Lease(connections)  # one per run, from its first attempt to its last
await lease.list_tools(server)  # the server's own tool list, unprefixed and unfiltered
await lease.session(server)  # the session the tools call through
await registry.add_mcp(server, lease)  # the agent's view: allow-list, prefix, timeout, cap, failures as text
await lease.release()  # the run is done
```

## Connections and lifecycle

- Nothing connects when the runner is created. A server opens when a run first needs it.
- Each server is held by an **owner task**: it opens the session, publishes it, and waits for a stop signal. Calls from
  any task share the session.
- Concurrent first uses open one connection (a lock per server).
- **Connections are shared** between agents whose connection settings are equal: the command, args, cwd and environment
  names for stdio; the URL and header names for HTTP.
- **A run holds a lease** on its servers from its first attempt to its last, so a retry does not start a server again. A
  server is stopped when its last lease is released.
- **The context manager is optional.** `async with AgentRunner(db)` (or `aclose()` at the end) keeps servers up between
  runs and stops them at the end of the block. Without it, each run starts what it needs and stops it when it is done,
  unless another run still holds it. Nothing raises either way, and a runner needs no setup to run an agent with MCP
  servers, also when the agent is launched by another agent.
- An attempt gets its servers before it takes a concurrency slot, so a slow start (`npx`) does not hold one.
- `aclose()` stops every server, whatever holds it, and can be called twice. A later run starts them again.
- **A dead connection is replaced.** When a call finds the connection broken, or its owner task ends, it is forgotten,
  and the next attempt that needs the server opens a new one. The runner's retries therefore cover a crashed server.

## Failures are values

| Case | Result |
|---|---|
| Missing environment variable, command not found, an allow-listed tool the server lacks, a name collision | The attempt fails at once, **without retry**. The message names the server (never a secret's value) |
| The connection fails to open for another reason | An ordinary failed attempt, which is retried |
| The tool returns an MCP error (`isError`) | Error text the model can read (the adapter's `handle_tool_errors`) |
| The server dies, or the connection drops, during a call | The model gets `MCP server 'x' is unavailable: ...`. The connection entry is cleared |
| A call exceeds `timeout_s` | The model gets `The call to 'x_tool' timed out after 60s` |
| Bad arguments | The server validates them and its error text reaches the model |

Every MCP tool is wrapped when the registry is built, so that no exception from the adapter or the connection reaches
the agent loop. Without the wrapper a dead server ends the whole run (checked).

## What reaches the model

- **Text, images and embedded files pass through** as the adapter converts them. Checked with `gemini-3.5-flash-lite`: it
  named the colour of a returned PNG, and read a code out of a returned PDF.
- **A resource link is turned into text** (`Resource link: <uri> (<mime type>)`), not passed through. The adapter makes
  it a file block with a URL, and the Gemini client then **fetches that URL itself**, when the next model call is
  prepared. That has two problems: a server can make our process fetch any address, and a failed fetch raises outside
  the tool, so the tool wrapper cannot catch it (checked: a 404 killed the run).
- **Audio** makes the adapter raise. The wrapper turns that into text: `The tool returned audio, which is not supported`.
- **`structuredContent`** is not shown to the model separately: the adapter puts it in the message's artifact, and a
  server that declares an output schema must also return the same data as text. If a result has **no content but has
  structured content**, the registry renders the structured content as JSON text.
- **Size.** The text of a result is capped at the server's `max_result_chars` (default 20,000 characters, about 5,000
  tokens). Longer text is cut at the limit and ends with `[truncated: 48,213 characters, showing the first 20,000]`. The
  cap is per result, counted over all its text blocks together. It applies to text only: images and embedded files are
  not capped in this version. A result counts in the next model call's input tokens, and an oversized one can exceed the
  context limit and fail the attempt.

## Logging

- Each call logs `mcp_call attempt_id=... server=... tool=... status=ok|error|timeout ms=...`. Arguments and results
  are not logged.
- Opening and closing log `mcp_open server=... transport=...` and `mcp_close server=...`.
- No new tables or columns.

## Not in this version

- Discovery of tools other than MCP's ([tool-discovery.md](tool-discovery.md)).
- SSE and websocket transports.
- OAuth flows for HTTP servers; MCP resources, prompts and sampling.
- A refresh of a server's tool list. It is read when a connection is opened.
- Using tool annotations (`destructiveHint` and so on) as policy.

## Tests

A real stdio fake server (FastMCP, about 0.3s to start) with a scripted fake chat model, so no API calls.

- Tools appear named `server_tool` and are bound; a local tool and an MCP tool work in one attempt.
- Concurrent calls share one session and one process.
- A retry does not start the server again.
- Two agents with equal connection settings share a process; with different settings, they do not.
- A server that dies mid-call gives text to the model, and the next attempt gets a new connection.
- A call over `timeout_s` gives text.
- Each setup failure fails at once, with one attempt.
- A name collision is refused. A typo in an allow-list is refused.
- A secret's value is in no log line, run row or exception message.
- A server stops when the run ends without `async with`, and when the block ends with it; two runs share a server inside
  the block, and parallel runs share one outside it. `aclose()` leaves no child process; closing twice is fine.
- An MCP agent runs on a runner that is not open (no `async with`), and its server is stopped when the run ends.
- A resource link reaches the model as text, with no fetch. Audio becomes a text note.
- An image and a PDF returned by the server reach the real model and are read (e2e, `gemini-3.5-flash-lite`).
- Unit tests for the empty-content-with-structured-content rule, and for the cap: a result at the limit is untouched,
  one over it is cut with the note, and several text blocks are counted together.
- One e2e test against the real model and the fake server, and, if Node is available, one against a small real server.

## Decisions

- The runner's context manager is **optional**: a server starts when a run needs it and stops when the last run using it
  ends, unless the runner is inside `async with`. (First decided the other way, that an MCP agent on a runner that was not
  open raises. That made the rule depend on the whole tree of launched agents, and it was dropped.)
- Connections are shared and per runner; the registry is per attempt and owns nothing. The agent's declaration is applied on
  the registry side.
- `tools` not given means every tool of the server; given, only those. An allow-list is therefore optional per server.
- Images and embedded files pass through; resource links become text (see the check above).
- Text results are capped, per server, at `max_result_chars` (default 20,000).

