# Recipes

Short, complete patterns to adapt, for building with featherweight. Each one says what it is for and runs as written; what is yours (a store, a directory, a web handler) is named in its code, and a recipe that uses the agent of [Getting started](../getting-started.md) says so. The tests run all of them. The guides say how each part works, and the specs say what it does and why; a recipe puts a few parts together for one job.

| I want to | Recipe |
|---|---|
| [A pipeline of two agents](a-pipeline-of-two-agents.md) | A job that runs agents in plain Python, with a typed input and output |
| [Resuming a failed job](resuming-a-failed-job.md) | Pick up where a failed job stopped, without paying for the steps that worked |
| [Telling a retry what went wrong](telling-a-retry-what-went-wrong.md) | Feed the last error to the next attempt |
| [Testing with a fake model](testing-with-a-fake-model.md) | A whole test file with a scripted fake chat model, and no real call |
| [An agent with an MCP server](an-agent-with-an-mcp-server.md) | Give an agent the tools of an MCP server |
| [An agent that launches another](an-agent-that-launches-another.md) | A sub-agent on an allow-list, with limits |
| [A streaming agent for chat](a-chat-agent-that-streams.md) | Stream a chat answer to a browser |
| [A result store of your own](a-result-store-of-your-own.md) | Keep job results where you decide, and check the store with the kit |
| [A job that a worker runs](a-job-that-a-worker-runs.md) | Submit a job, run it on a worker, and follow it |
| [Traces for jobs and agents](traces-for-jobs-and-agents.md) | OpenTelemetry spans for jobs, attempts, model calls and tool calls |
| [Giving a sub-agent context](giving-a-sub-agent-context.md) | Let the parent write a summary of what it knows into the sub-agent's input |
| [An agent that starts another and goes on](an-agent-that-starts-another-and-goes-on.md) | A background job whose result comes back later |
| [A different provider](a-different-provider.md) | Use another model or provider |
| [A skill from a database or S3](a-skill-from-a-database-or-s3.md) | Keep a skill's text in your own storage, and read it when the agent asks |
| [A skill for each department](a-skill-for-each-department.md) | One agent, with the skills of whoever is asking |
| [Agents built from JSON, with your own rules](agents-built-from-json.md) | Let users define agents from a catalog you control, and run them on workers |
| [A queue that signs its messages](a-queue-that-signs-its-messages.md) | Wrap any queue with an HMAC signature, and check it with the kit |
| [Approving what an agent proposes](approving-what-an-agent-proposes.md) | An agent proposes, a person approves, plain code executes once |
| [Asking the user in the middle of a job](asking-the-user-in-the-middle-of-a-job.md) | A job ends with its questions, and the next job reuses the finished stage |
| [A tool that gets a token for its identity](a-tool-that-gets-a-token-for-its-identity.md) | Act for the signed-in user: check, then get a credential at each call |
| [Files for an agent or a job: pass a reference](files-for-an-agent-or-a-job.md) | Keep the file in your store, pass its reference, and let a tool fetch it and check the identity |

The skill for coding agents carries an abridged copy of these pages, built from them by `scripts/build_skill.py`, so edit the pages here.
