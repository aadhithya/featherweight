# An agent with an MCP server

Give an agent the tools of an MCP server. The agent below answers questions about a GitHub repository with the tools of GitHub's read-only server.

```python
import asyncio
from typing import ClassVar

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    HttpMCPServer,
    ModelSettings,
    SqliteDatabase,
    default_db_path,
)


class Question(AgentInput):
    question: str


class Answer(AgentOutput):
    answer: str = ""


class RepoReader(AgentBase):
    name: str = "repo_reader"
    description: str = "Answers questions about a GitHub repository"
    version: str = "0.1.0"
    prompt: str = "Answer from the repository, using the tools."
    model: ModelSettings = Gemini()
    input_model: ClassVar[type[AgentInput]] = Question
    output_model: ClassVar[type[AgentOutput]] = Answer
    mcp_servers: list = [
        HttpMCPServer(
            name="github",
            url="https://api.githubcopilot.com/mcp/readonly",
            bearer_from="GITHUB_TOKEN",  # the NAME of the variable, not the token
            description="Repositories, issues and code",
        )
    ]


async def main() -> None:
    async with (
        SqliteDatabase(default_db_path()) as db,
        AgentRunner(db) as runner,
    ):  # keeps the server connections open between runs
        result = await runner.run(RepoReader, Question(question="What does aadhithya/featherweight do?"))
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

`pip install "featherweight[mcp]"` and set `GITHUB_TOKEN`. A server that has many tools can be searched instead of listed: [the MCP guide](../mcp.md).
