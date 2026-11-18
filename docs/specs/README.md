# Specs

What each part of `micro` is supposed to do, and why. ([Documentation home](../README.md) shows how these fit with the guides.) The pages in [`docs`](..) say how to use it; these say what was
decided, what was checked, and what is deliberately left out. Each follows the same shape: goal, the rules, decisions,
not supported, tests.

| Spec | Covers |
|---|---|
| [Agents](agents.md) | The agent definition, what is checked when it is built, input and output, the system prompt |
| [The agent runner](runner.md) | Running, results, attempts and retries, concurrency, **launching agents**, limits |
| [Models and providers](models.md) | `ModelSettings`: Gemini, Gemma, Claude and OpenAI, the catalog, temperature, extending it, tokens |
| [Jobs and tracing](jobs-and-tracing.md) | Jobs, the run log, statuses, logging |
| [Skills](skills.md) | The skill model, loading, how an agent reads a skill, the Agent Skills specification |
| [Tools, the registry and the store](tools-and-store.md) | Tools on an agent, the tool registry, the ready-made tools, the run context, the run store |
| [Tool discovery](tool-discovery.md) | Finding MCP tools by keyword: `discover`, `search_tools`, `call_tool`, matching |
| [MCP tools](mcp-tools.md) | MCP servers: declaring, connections, failures, what the model gets |
