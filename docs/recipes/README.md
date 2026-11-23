# Recipes

Short, complete patterns to adapt, for building with featherweight. They use the agent of [Getting started](../getting-started.md) (`PeopleExtractor`). The guides say how each part works, and the specs say what it does and why; a recipe puts a few parts together for one job.

| I want to | Recipe |
|---|---|
| [A pipeline of two agents](a-pipeline-of-two-agents.md) | A job that runs agents in plain Python, with a typed input and output |
| [Resuming a failed job](resuming-a-failed-job.md) | Pick up where a failed job stopped, without paying for the steps that worked |
| [Telling a retry what went wrong](telling-a-retry-what-went-wrong.md) | Feed the last error to the next attempt |
| [Testing with a fake model](testing-with-a-fake-model.md) | A whole test file with a scripted fake chat model, and no real call |
| [An agent with an MCP server](an-agent-with-an-mcp-server.md) | Give an agent the tools of an MCP server |
| [An agent that launches another](an-agent-that-launches-another.md) | A sub-agent on an allow-list, with limits |
| [A chat agent that streams](a-chat-agent-that-streams.md) | Stream a chat answer to a browser |
| [A result store of your own](a-result-store-of-your-own.md) | Keep job results where you decide, and check the store with the kit |
| [A job that a worker runs](a-job-that-a-worker-runs.md) | Submit a job, run it on a worker, and follow it |
| [Traces for jobs and agents](traces-for-jobs-and-agents.md) | OpenTelemetry spans for jobs, attempts, model calls and tool calls |
| [An agent that starts another and goes on](an-agent-that-starts-another-and-goes-on.md) | A background agent whose result comes back later |
| [A different provider](a-different-provider.md) | Use another model or provider |

The skill for coding agents carries an abridged copy of these pages, built from them by `scripts/build_skill.py`, so edit the pages here.
