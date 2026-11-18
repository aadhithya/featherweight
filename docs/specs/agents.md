# Spec: agents

Status: implemented. How to use it is in [agents.md](../agents.md). The code is [models.py](../../src/micro/models.py).

- [Goal](#goal)
- [The definition](#the-definition)
- [Checked when an agent is built](#checked-when-an-agent-is-built)
- [Input and output](#input-and-output)
- [The system prompt](#the-system-prompt)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)

## Goal

An agent is a **static, typed definition**. Pydantic is the specification: the input, the output, the tools, the
skills, the sub-agents and the MCP servers are declared on the class and enforced by Pydantic, not by convention. Nothing
about an agent is decided at run time, so it can be read, tested and versioned like any class. A malformed definition
fails when it is built, before any model is called.

## The definition

A subclass of `AgentBase`. The runner instantiates it (`agent_cls()`), so a subclass supplies defaults for every field.

| Field | Type, default | Rule |
|---|---|---|
| `name`, `description`, `version`, `prompt` | `str`, required | `name` and `version` are recorded on every attempt. `description` is what a parent's model reads before it launches this agent |
| `model_name` | `Literal["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]`, `gemini-3.5-flash-lite` | Another value fails validation |
| `reasoning` | `Literal["low", "medium", "high"]`, `medium` | The model's thinking level |
| `tools` | `list[BaseTool]`, empty | Only these are bound to the model, besides the tools the runner adds ([tools and store](tools-and-store.md)) |
| `recursion_limit` | `int`, 25, at least 1 | Passed to langgraph. Stops a runaway tool loop inside one run; a run that hits it fails |
| `allow_user_questions`, `raise_conflicts` | `bool`, `False` | Each only adds a sentence to the prompt. Neither is enforced |
| `subagents` | `list[type]`, empty | Agent classes this agent may launch ([runner](runner.md#launching-agents)) |
| `skills` | `list[Skill]`, empty | Skills this agent can read ([skills](skills.md)) |
| `mcp_servers` | `list[StdioMCPServer \| HttpMCPServer]`, empty | MCP servers whose tools this agent may use ([MCP tools](mcp-tools.md)) |
| `input_model`, `output_model` | `ClassVar`, `AgentInput` and `AgentOutput` | The agent's contract |

A subclass re-annotates a field it overrides (`name: str = "x"`), because Pydantic rejects an unannotated override.

## Checked when an agent is built

Each of these raises a `ValidationError` (a programming error, not a failed run):

- `subagents`: every entry is an `AgentBase` subclass; none is the agent's own class; the names of the sub-agents are
  distinct.
- `skills`: the names are distinct.
- `mcp_servers`: the names are distinct. Each server model checks itself too (name format, timeouts, caps).
- `tools`: the names are distinct, and none is `launch_agent`, `get_agent_schema` or `get_skill`, which the runner uses
  for its own tools.
- `model_name`, `reasoning`, `recursion_limit`: the allowed values above.

Agent A listing B while B lists A is allowed. The runner's depth limit stops it.

## Input and output

- **`AgentInput`** forbids extra fields (`extra="forbid"`). The runner raises `TypeError` for an input that is not an
  instance of the agent's `input_model`: a wrong type is a bug, so it raises. The input is sent to the model as indented
  JSON, in a single user message.
- **`AgentOutput`** always carries `open_questions: list[str]`, where a model reports a question or a conflict instead
  of guessing. It is never a failure. The task fields of an output should have defaults, so that a model that needs to
  ask is not forced to invent an answer to pass validation.
- **The output is enforced twice.** The output model is the structured-output schema the model is given
  (`response_format`), and the reply is validated against it again. A reply that does not validate is a failed attempt.
- **`check_output(agent_input, output)`** is the place for rules between the input and the output ("everything the model
  returned is in the input"). It raises `ValueError` to reject, which is a failed attempt and is retried. The default
  accepts everything. Rules about the output alone belong in validators on the output model.

## The system prompt

Assembled by `build_system_prompt(can_launch=True)`, in this order, and joined by blank lines:

1. `prompt`
2. one sentence for `allow_user_questions`, whichever way it is set, and one for `raise_conflicts`
3. if the agent has `subagents` and `can_launch`: the sub-agents by name and description, and how to use them. Their
   schemas are not in the prompt
4. if the agent has `skills`: each skill's name and description, and how to read one

The runner sets `can_launch` to false for an agent at the depth limit. Its SHA-256 is recorded on every attempt
(`system_prompt_hash`), so two runs can be compared by prompt. MCP tools add nothing to the prompt.

## Decisions

- **Static, and classes, not instances.** A definition has no per-run state. A run passes the class and the input, and
  the runner builds the agent. A different prompt is a different class, with a new `version`.
- **The instruction flags are not enforced.** `allow_user_questions` and `raise_conflicts` only tell the model what it
  may do. An output that asks a question when asking is forbidden is not rejected.
- **A failing rule is the pipeline's, not the agent's.** `check_output` only rejects. It cannot fix the output or decide
  what a failure means: the pipeline does.
- **Flat output schemas for Gemini.** A nested or nullable structured-output schema can come back silently truncated (a
  tree with only its root). Prefer flat lists with ids, avoid `X | None` on nested objects, and add a `check_output` that
  compares the output with the input. The example pipeline builds its trees from flat lists for this reason.
- **The model is a closed set.** `model_name` is a `Literal`, so a typo fails at build time. Other providers are
  [issue #2](https://github.com/aadhithya/micro/issues/2).

## Not supported

- Per-run overrides of the prompt, model or tools.
- Models other than the three Gemini models, and provider-specific settings (a thinking budget).
- Enforcing `allow_user_questions` and `raise_conflicts`.
- Output validation that feeds the error back to the model. A retry resamples with the same input ([runner](runner.md)).
- Streaming.

## Tests

[test_models.py](../../tests/test_models.py) covers the defaults, the allowed models and reasoning levels, the prompt flags
and the prompt hash; [test_runner.py](../../tests/test_runner.py) covers input type errors, output validation and
`check_output`; the build-time checks are in [test_subagents.py](../../tests/test_subagents.py) (sub-agents),
[test_skills.py](../../tests/test_skills.py) (skills), [test_mcp.py](../../tests/test_mcp.py) (servers and the reserved tool
names) and [test_registry.py](../../tests/test_registry.py) (duplicate tools).
