# MCP tools

A `RepoReader` agent that answers questions about public GitHub repositories. It has no tools of its own: it uses the
tools of GitHub's MCP server.

```
make example-mcp                                  # two sample questions
make example-mcp ARGS="your question"
```

Without Docker: `uv run python examples/mcp/agent.py "your question"`.

## Before you run it: a GitHub token

GitHub's MCP server needs a token, even to read public repositories. Use a **fine-grained personal access token that can
only read public data**. There is no command for creating one (the `gh` CLI cannot), so it is made on the website:

1. Open <https://github.com/settings/personal-access-tokens/new>.
2. **Token name:** `micro-mcp-example`. **Expiration:** 7 days, or whatever you are comfortable with.
3. **Repository access:** *Public repositories (read-only)*. Leave every permission empty. Public data needs none.
4. Generate it, copy the token (it starts with `github_pat_`) and add it to `.env`:

```
GITHUB_TOKEN=github_pat_...
```

`.env` is gitignored. The token is read from the environment when the server is connected, and it is not in the agent's
definition, a log line or the run log.

## What happens

```python
GITHUB = HttpMCPServer(
    name="github",
    url="https://api.githubcopilot.com/mcp/readonly",
    bearer_from="GITHUB_TOKEN",
    tools=(
        "search_repositories",
        "get_file_contents",
        "list_issues",
        "issue_read",
        "list_releases",
        "get_latest_release",
    ),
)
```

- **Three layers keep it read-only.** The `/readonly` URL offers no write tools, the `tools` allow-list lets the agent see
  six of the dozens that are left, and the token itself can only read public data.
- **The tools arrive named after the server:** `github_list_releases`, and so on. They are bound to the model next to
  any tools the agent has of its own, with the schemas the server publishes.
- **The runner connects on first use**, before the model is called. The script runs the agent inside
  `async with AgentRunner(db) as runner` so that the connection stays up for the second question instead of starting
  again, and closes at the end. Without the block it works the same, and each request starts and stops the connection.
- **A tool that errors is text for the model.** If GitHub says a repository does not exist, the agent reads that and
  answers accordingly. It does not crash the run.

Each request is a job in `micro.db`. `select tool_names from agent_runs` shows the six tools, and the log shows each call:
`mcp_call ... server=github tool=github_get_latest_release status=ok ms=...` (run with `MICRO_LOG_LEVEL=INFO`).

## Things to try

- Ask about a repository that does not exist, and see the agent say so.
- Remove `get_file_contents` from `tools` and ask what a repository's README says: the agent can no longer read files.
- Use a wrong tool name in `tools` (`"list_issue"`): the run fails at once, naming the tools the server does have.
- Unset `GITHUB_TOKEN`: it fails at once, naming the variable.

## Where to look

[`agent.py`](agent.py) is the agent (about 40 lines) and the script. What is supported, how failures are handled and the
connection rules are in [docs/mcp.md](../../docs/mcp.md).
