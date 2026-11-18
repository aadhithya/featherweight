# Examples

Each folder here is an example you can run. They use the real model, so they need `GOOGLE_API_KEY` in `.env` (copy
`.env.example`) and make real, small API calls. `mcp` also needs a read-only `GITHUB_TOKEN`. They all use `gemini-3.5-flash-lite`, except the `doclearn` agents,
which use `gemini-3.8-flash` for the harder steps.

| Folder | What it shows | Run it |
|---|---|---|
| [`launching`](launching) | One agent launching other agents: an allow-list, the two tools, and the run tree in the trace | `make example-launching` |
| [`mcp`](mcp) | An agent that uses the tools of an MCP server (GitHub's, read-only), with an allow-list and a token read by name | `make example-mcp` |
| [`skills`](skills) | An agent that reads skills (the Agent Skills format) when a request needs them | `make example-skills` |
| [`doclearn`](doclearn) | A whole pipeline: learn a template from example documents, then write a new document from it | `make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"` |

`launching`, `skills` and `mcp` are one file of agents plus a script that runs them, and they are the place to start. `doclearn`
is bigger: five agents, two pipelines, sample documents and its own tests.

The examples print answers only. `MICRO_LOG_LEVEL=INFO make example-mcp` (or any other example) also prints the log: each
attempt, tool call, search and launch. `doclearn` has its own: `-v`.

Each `make` target runs in Docker. Without Docker, run the script with `uv run python examples/<folder>/agent.py`
(`doclearn` is run as a module: see its README). Pass your own request after the script, or as `ARGS="..."` to `make`.

The documentation for what they use is in [`docs`](../docs): [agents](../docs/agents.md),
[launching agents](../docs/launching-agents.md), [MCP tools](../docs/mcp.md), [skills](../docs/skills.md) and the
[example pipeline](../docs/example-pipeline.md).
