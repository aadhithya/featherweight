# An agent with an MCP server

```python
from featherweight import HttpMCPServer


class RepoReader(AgentBase):
    name: str = "repo_reader"
    description: str = "Answers questions about a GitHub repository"
    version: str = "0.1.0"
    prompt: str = "Answer from the repository, using the tools."
    mcp_servers: list = [
        HttpMCPServer(
            name="github",
            url="https://api.githubcopilot.com/mcp/readonly",
            bearer_from="GITHUB_TOKEN",  # the NAME of the variable, not the token
            description="Repositories, issues and code",
        )
    ]
```

`async with AgentRunner(db) as runner:` keeps the server connections open between runs and closes them at the end. It is
optional.
