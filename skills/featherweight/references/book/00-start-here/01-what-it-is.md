<!-- Copied from docs/book/00-start-here/01-what-it-is.md by scripts/build_skill.py. Edit the original, not this file. -->

# What featherweight is

An opinionated agent harness for Python. **Pydantic is the spec. Everything else follows.**

You declare an agent as a typed class: its input, its output, its prompt, and what it may use. featherweight runs it, checks what
comes out, retries what fails, and records what happened. You write the part that is yours: the pipeline around it.

## What it does

- **Typed agents.** What goes in and what comes out is a schema. A bad answer is caught and retried before your code sees it.
- **Abilities.** Tools, MCP servers, skills and sub-agents, declared on the agent.
- **Jobs.** A pipeline is a job: a declared class that runs in your process or on a worker, with a status, events and a result.
- **Chat.** A streaming agent, for a browser or a terminal.
- **Background agents.** An agent starts another agent and goes on.
- **Traces.** OpenTelemetry spans for jobs, attempts, model calls and tool calls.

## What it is not

- **Not a workflow engine.** A pipeline is plain Python: `if` and `for`, no graph to draw.
- **Not a store for your data.** The framework stores metadata and nothing else. You bring the storage.
- **Not a server or a UI.** It runs no server. You send its events wherever you like.

Want durable execution or built-in evals? Look at Pydantic AI. Want a graph that pauses and resumes? Look at LangGraph.

## What you will build

A small incident report assistant: a report goes in, a structured summary comes out. It grows through the book, from one agent to a
chat and a pipeline on workers. See [how to read this book](04-how-to-read.md).
