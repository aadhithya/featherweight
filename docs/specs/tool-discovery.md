# Spec: tool discovery

Status: implemented. How to use it is in [mcp.md](../mcp.md#finding-tools-by-search). This is the design for [issue #3](https://github.com/aadhithya/featherweight/issues/3). It builds on
[MCP tools](mcp-tools.md) and [tools, the registry and the store](tools-and-store.md).

- [Goal and scope](#goal-and-scope)
- [Declaration](#declaration)
- [The registry](#the-registry)
- [What the model sees](#what-the-model-sees)
- [The two tools](#the-two-tools)
- [Matching](#matching)
- [Failures are values](#failures-are-values)
- [Logging](#logging)
- [Decisions](#decisions)
- [Not in this version](#not-in-this-version)
- [Tests](#tests)

## Goal and scope

An MCP server can offer dozens of tools, and binding all of them to the model puts every schema in every model call. With
discovery the model is bound to two tools only, `search_tools` and `call_tool`, finds the tools it needs by keyword, and
calls them through `call_tool`.

- **Only MCP tools are discoverable.** An agent's own `tools` are always bound directly.
- **It is on by default.** An MCP server is discoverable unless it is declared with `discover=False`, which binds its
  tools directly, as before.
- **Nothing is bound mid-run.** An agent's tools are fixed when the attempt starts. A found tool is used through
  `call_tool`.

## Declaration

```python
class MCPServer(BaseModel, frozen=True):
    description: str = ""  # what the server is for; the model reads it, to know what to search for
    discover: bool = True  # False: bind this server's tools directly
```

`discover` belongs to the agent's use of a server, like `tools`. It is not part of the connection key, so two agents can
share a server and differ on it. Use `discover=False` for a small server, where one native call is better than a search and
a call. `description` should say what the server is for (`"Issues, pull requests and code search"`). It is optional: without
it the prompt names the server only, and the search still matches on the tool descriptions.

## The registry

Each entry now has a **mode**, `direct` or `discoverable`, and the name of its server.

- `add(tool, source, mode="direct", server="")`; `add_mcp` adds a server's tools as `discoverable` when `server.discover`.
- **Names are unique across both modes.** A discoverable tool and a direct tool cannot share a name.
- `tools` and `names` are the **bound** entries, the direct ones. `discoverable` lists the others.
- `search(keywords, k)` ranks the discoverable entries ([Matching](#matching)).

The runner binds `registry.tools`, and adds `search_tools` and `call_tool` when the agent has a discoverable server. They
are reserved names, like `launch_agent` and `get_skill`. `tool_names` in the run log lists the bound tools, so a discovery
agent shows `search_tools` and `call_tool`, and not the tools behind them.

## What the model sees

An agent with at least one server that has `discover=True` gets a section in its system prompt, built from the
declaration, so the prompt and its hash are known before any server is started:

```
You can find more tools by searching. These servers have tools you can search for:
- github: Issues, pull requests and code search
- docs
Call `search_tools` with a few keywords to find the tools you need. Each result has the tool's name, description and input
schema. Call a tool you found with `call_tool`, passing its name and its input as a JSON string. Search results stay in this
conversation: search again only when the tool you need is not in an earlier result.
```

## The two tools

Built for each attempt, by the runner, over the registry. Both return text, or content blocks for a result, and never
raise into the run.

**`search_tools(keywords: list[str])`**

- Returns up to 5 hits, best first, as JSON: for each, `name`, `server`, `description` and `input_schema` (the schema the
  server published).
- A search with no keywords, or no tokens in them, says to give at least one.
- **No match** says so, suggests different or broader keywords, and lists the servers it can search.

**`call_tool(name: str, args_json: str)`**

- `name` is a discoverable tool of this agent. `args_json` is a JSON **string** (Gemini can reject an object parameter that
  has no declared properties, as with `launch_agent`).
- It does **not** require an earlier search. The allow-list is the boundary: any discoverable tool the agent may use can be
  called by name.
- The arguments are validated against the tool's real schema (`jsonschema`) before the call. This avoids the loss in
  Gemini's schema conversion that direct binding has ([MCP tools](mcp-tools.md#findings-from-the-adapter)).
- The call goes through the same wrapper as a direct MCP tool: the timeout, the size cap, and failures as text.

| Case | What comes back |
|---|---|
| The tool runs | What a direct call would give: text, images, files |
| `args_json` is not a JSON object | `The input is not valid JSON ...` |
| The arguments fail the schema | The first errors (path and message), and the tool's **input schema**, so the model can fix the call without searching again |
| An unknown name | `'x' is not a tool you can call. Search for it with search_tools.` |
| The name of a direct tool | `'x' is bound to you directly: call it as 'x'.` |

## Matching

A small BM25 in `featherweight/search.py`, with no dependency. It is generic: it takes texts and a query and returns ranked
indices, so skill and agent lookup can use it later.

- **Tokens:** lowercase letters and digits. Underscores, punctuation and a lower-to-upper camelCase boundary split words.
  There is no stemming.
- **What is indexed per tool:** its name, twice (the name is the strongest signal, and it carries the server prefix), its
  description, and the names of its arguments.
- **Scoring:** BM25 with `k1 = 1.5` and `b = 0.75`, and an idf that is always positive. A tool scores only if it matches
  at least one keyword. Ties keep the order the tools were added in.
- All keywords are searched together as one query.

## Failures are values

Everything above returns text. A server that dies while a tool is called is the MCP wrapper's case: the model gets
`MCP server 'x' is unavailable`, and the next attempt opens a new connection. A server that cannot start fails the attempt
at once, as without discovery.

## Logging

`tool_search agent_run_id=... keywords=... hits=...` for each search, and
`tool_call_via_search agent_run_id=... tool=... status=ok|error|invalid` for each call, on top of the `mcp_call` line the
wrapper already writes. Keywords are logged. Arguments and results are not.

## Decisions

- **Opt-out, and per server.** A big server is the common reason for MCP, and binding all its tools is the costly default.
  A small server opts out with one field.
- **The prompt names servers, not tools.** The model needs to know what exists to know what to search for, without the tools
  costing anything until they are found.
- **No "found first" rule.** The allow-list is the boundary. Enforcing a prior search would add state and fail legitimate
  calls. The prompt says to search only when the tool is not in an earlier result, and a bad call returns the schema.
- **Nothing is bound mid-run**, so an attempt's tools stay what the definition and the registry say.
- **Our own BM25.** A few hundred short texts do not need a dependency or an index that is kept between calls.
- **The same wrapper for both paths**, so the timeout, the cap and failures-as-text behave the same direct or discovered.

## Not in this version

- Discovery of an agent's own tools, of skills and of agents. Skill and agent lookup are planned on the same matcher.
- Semantic (embedding) search, stemming, and typo tolerance.
- Pagination of search results, and a setting for how many hits (5).
- Remembering which tools an agent found, between runs.

## Tests

A real stdio fake server with a scripted model: a search ranks the right tools and names them with their schemas; a call
validates and runs; a bad call returns the schema; no match; unknown and direct names; an agent with `discover=False` binds
directly and gets no discovery tools; a mix of both on one agent; the prompt section and its hash; reserved names;
`tool_names` in the log. Unit tests for the matcher. A real-model test, with `gemini-3.5-flash-lite`, that finds and calls a
tool.
