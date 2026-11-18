# Examples

Each folder here is an example you can run. They use the real model, so they need `GOOGLE_API_KEY` in `.env` (copy
`.env.example`) and make real, small API calls. They all use `gemini-3.5-flash-lite`, except the `doclearn` agents,
which use `gemini-3.8-flash` for the harder steps.

| Folder | What it shows | Run it |
|---|---|---|
| [`launching`](launching) | One agent launching other agents: an allow-list, the two tools, and the run tree in the trace | `make example-launching` |
| [`skills`](skills) | An agent that reads skills (the Agent Skills format) when a request needs them | `make example-skills` |
| [`doclearn`](doclearn) | A whole pipeline: learn a template from example documents, then write a new document from it | `make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"` |

`launching` and `skills` are one file of agents plus a script that runs them, and they are the place to start. `doclearn`
is bigger: five agents, two pipelines, sample documents and its own tests.

Each `make` target runs in Docker. Without Docker, run the script with `uv run python examples/<folder>/agent.py`
(`doclearn` is run as a module: see its README). Pass your own request after the script, or as `ARGS="..."` to `make`.

The documentation for what they use is in [`docs`](../docs): [agents](../docs/agents.md),
[launching agents](../docs/launching-agents.md), [skills](../docs/skills.md) and the
[example pipeline](../docs/example-pipeline.md).
