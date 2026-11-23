# Spec: tools and the tool registry

Status: implemented. How to use them is in [agents.md](../agents.md#tools). The code is [tools.py](../../src/featherweight/tools.py),
[registry.py](../../src/featherweight/registry.py) and [context.py](../../src/featherweight/context.py). (The file keeps its old name, `tools-and-store.md`, so that links to it still work: there is no store any more.)

- [Goal](#goal)
- [Tools on an agent](#tools-on-an-agent)
- [The tool registry](#the-tool-registry)
- [Ready-made tools](#ready-made-tools)
- [The run context](#the-run-context)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)

## Goal

An agent has exactly the tools its definition lists, and the runner adds to them only what the definition asks for.
Every tool of an attempt is checked together before the model sees any of them. A tool never throws at the model: it
returns text the model can read.

## Tools on an agent

`tools: list[BaseTool]`. A function decorated with langchain's `@tool` works. **Only these tools are bound to the model**,
apart from the ones the runner adds:

| The agent declares | The runner adds | The prompt gains |
|---|---|---|
| `subagents` (and depth is left) | `launch_agent`, `get_agent_schema` | The agents it may launch |
| `skills` | `get_skill` | The skills it has |
| `mcp_servers` | `<server>_<tool>` for each tool of each server of `discover=False` | Nothing |
| `mcp_servers` (the default, `discover=True`) | `search_tools`, `call_tool` | The servers it can search |

The user does not list these. Their names (`launch_agent`, `get_agent_schema`, `get_skill`, `search_tools`, `call_tool`) are reserved: an agent with its own tool of one of those names fails when it is
built ([agents](agents.md#checked-when-an-agent-is-built)). The run log's `tool_names` lists everything bound.

## The tool registry

Internal. One per attempt of one agent. It holds the tools of that attempt, with where each came from (`local`, `mcp` or
`runner`), and owns nothing live: connections are the runner's and are shared ([MCP tools](mcp-tools.md)).

- **Adding a tool** raises `SetupError` when the name is already taken (the message says by which sources), or is a name
  the model could not be given: it must start with a letter or an underscore, use letters, digits and `_ . : -`, and
  have at most 64 characters (Gemini's rule).
- **`add_mcp(server, lease)`** adds the tools of one MCP server as the agent declares it: the allow-list, the `<server>_`
  prefix, the timeout and the size cap.
- **A setup error is not retried.** The run fails at once and the message names the tools.
- The registry's `tools` are what `create_agent` binds, and its `names` are what the log records.

Each entry has a **mode**: `direct` (bound to the model) or `discoverable` (found with `search_tools` and run with
`call_tool`; only MCP tools, by default). `tools` and `names` are the bound ones, names are unique across both modes, and
`search(keywords, k)` ranks the discoverable ones ([tool discovery](tool-discovery.md)).

## Ready-made tools

In `featherweight.tools`. All return text, never raise, and the model can read what went wrong.

| Tool | |
|---|---|
| `calculate(expression)` | **Exact** decimal arithmetic: numbers and `+ - * / // % **` with parentheses. At most 200 characters, and an exponent at most 100. Division by zero, an invalid operation, overflow and bad syntax return `Error: ...`. No functions, names or other syntax |
| `current_datetime()` | The current UTC time, ISO 8601 |
| `add_to_date(date, days, weeks, months, years)` | `date` is `YYYY-MM-DD` or `today`. Months and years are calendar-aware and clamp to the end of the month (Jan 31 plus 1 month is Feb 28 or 29) |
| `days_between(start, end)` | Whole days, negative if `end` is before `start` |

## The run context

A tool that needs the job declares `runtime: ToolRuntime[RunContext]`. Langchain fills it in and hides it from
the model, so the model never passes a job id. `RunContext` has `job_id`, `agent_run_id` (the attempt that is
running) and `identity`: who the run is for, if the application gave one. `RunContext` is generic in the identity,
so a tool declares `ToolRuntime[RunContext[Teacher]]` and reads `runtime.context.identity`. The [identity spec](identity.md#how-tools-read-it) owns it.

The shared MCP tools cannot take a `RunContext`, because one tool object serves many attempts. They read
`CURRENT_AGENT_RUN_ID`, a context variable the runner sets around each attempt, to say in the log which attempt called them. The identity has one of its own for code that cannot take `runtime`: `CURRENT_IDENTITY`.

## Decisions

- **Allow-lists everywhere.** The agent lists its tools, sub-agents, skills and MCP servers, and gets nothing else.
- **Tools return text.** An error is a message the model can act on, in the same way a failed run is a value.
- **Exact arithmetic.** Totals are computed with `Decimal` by a tool, because a model does arithmetic badly.
- **The model never sees the job, or who the run is for.** Scope comes from the run context.
- **A registry per attempt, connections per runner.** What can differ between two agents belongs in the registry; what is
  shared is not.
- **No state is shared between agents.** There was a run store (an in-memory key-value store per job, with two tools); it was removed (issue #12). Agents pass information through
  their outputs and pipelines through return values, and an application that needs more keeps it in its own store (a dict keyed by job id for state that
  lives in one process, as the `doclearn` example does).

## Not supported

- State shared between agents, or kept by the framework between runs.
- Rows for tool calls in the run log.
- A per-call timeout or size cap for local tools. MCP tools have both.
- Policy on a tool, such as "needs approval" or read-only.
- Discovery of an agent's own tools.

## Tests

[test_calc_date_tools.py](../../tests/test_calc_date_tools.py) (`calculate` and the date tools, including errors),
[test_registry.py](../../tests/test_registry.py) (names, collisions, and the direct and discoverable modes), and
[test_mcp.py](../../tests/test_mcp.py) (`add_mcp`, reserved names).
