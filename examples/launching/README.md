# Launching agents

An `Editor` agent that gets help from two other agents: a `Summariser` and a `Translator`. It shows one agent launching
others, and how that appears in the trace.

```
make example-launching                      # two sample requests
make example-launching ARGS="your request"
```

Without Docker: `uv run python examples/launching/agent.py "your request"`.

## What happens

`Editor` lists the only agents it may launch: `subagents = [Summariser, Translator]`. The runner puts their names and
descriptions in its prompt and gives it two tools, `get_agent_schema` (to see what input an agent expects) and
`launch_agent` (to run it). The editor writes the headline itself, asks the summariser for the summary, and, if the
request asks for another language, asks the translator to translate.

After each request the script prints the runs of that job as a tree:

```
runs of this job:
  editor  attempt 1  SUCCESS  3654 in / 333 out
    summariser  attempt 1  SUCCESS  124 in / 49 out
    translator  attempt 1  SUCCESS  79 in / 23 out
```

Each indented run was launched by the one above it; that is recorded in the run log (`parent_agent_run_id`). To see the log
lines too (`agent_launch ...` for each launch), run `FTW_LOG_LEVEL=INFO make example-launching`.

## Things to try

- Ask for a headline in French, and then without a language: the editor launches the translator only when it is needed.
- Remove `Translator` from `subagents` and ask for French: the editor cannot launch it, so it has to cope without.
- Run with `MAX_AGENT_DEPTH=1`: launching is off, the editor gets no launch tools, and has to write the summary itself.

## Where to look

[`agent.py`](agent.py) has the three agents (about 70 lines) and the script that runs them and prints the tree. The behaviour, the limits and what is not
supported are in [docs/launching-agents.md](../../docs/launching-agents.md).
