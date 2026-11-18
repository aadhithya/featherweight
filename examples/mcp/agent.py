"""An agent that uses MCP tools, to run: it answers questions about public GitHub repositories with GitHub's own MCP server.

    python examples/mcp/agent.py                 # two sample questions
    python examples/mcp/agent.py "your question"  # your own question

Needs GOOGLE_API_KEY and GITHUB_TOKEN, a read-only token (see examples/mcp/README.md). The agent declares one server,
GitHub's remote one, and an allow-list of six read-only tools out of the dozens it offers. The runner connects when the
first request needs it, and `async with` keeps it up between the requests and closes it at the end.

Set MICRO_LOG_LEVEL=INFO (`MICRO_LOG_LEVEL=INFO make example-mcp`) to see the log lines: each server start, search and
tool call.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from micro.db import Database, default_db_path  # noqa: E402
from micro.log import setup_logging  # noqa: E402
from micro.mcp import HttpMCPServer, StdioMCPServer  # noqa: E402
from micro.model_settings import Gemini, ModelSettings  # noqa: E402
from micro.models import AgentBase, AgentInput, AgentOutput, RunStatus  # noqa: E402
from micro.runner import AgentRunner  # noqa: E402

GITHUB = HttpMCPServer(
    name="github",
    url="https://api.githubcopilot.com/mcp/readonly",  # the read-only variant of GitHub's server: no write tools at all
    bearer_from="GITHUB_TOKEN",  # the name of the variable; the token itself is never in the code
    discover=False,  # six tools are few enough to bind to the model; the default is to find them by search
    tools=(  # only these, whatever else the server offers
        "search_repositories",
        "get_file_contents",
        "list_issues",
        "issue_read",
        "list_releases",
        "get_latest_release",
    ),
)


class QuestionInput(AgentInput):
    question: str


class AnswerOutput(AgentOutput):
    answer: str | None = None
    sources: list[str] = []  # the GitHub pages the answer is based on


class RepoReader(AgentBase):
    name: str = "repo_reader"
    description: str = "Answers questions about public GitHub repositories"
    version: str = "0.1.0"
    prompt: str = (
        "Answer the question about public GitHub repositories, using your GitHub tools to look things up instead of "
        "answering from memory. Be brief: two or three sentences. Put the URLs of the pages you used in `sources`."
    )
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    mcp_servers: list[StdioMCPServer | HttpMCPServer] = [GITHUB]
    input_model: ClassVar[type[AgentInput]] = QuestionInput
    output_model: ClassVar[type[AgentOutput]] = AnswerOutput


SAMPLES = [
    "What is the latest release of the astral-sh/ruff repository, and when was it published?",
    "What are the three most recent open issues in the modelcontextprotocol/python-sdk repository?",
]


async def main(questions: list[str]) -> int:
    if os.environ.get("MICRO_LOG_LEVEL"):  # the log is for when you ask for it: the answers are what this prints
        setup_logging()
    failed = 0
    async with Database(default_db_path()) as db, AgentRunner(db) as runner:
        for question in questions:
            print(f"\n> {question}")
            job_id = await db.create_job("mcp-demo")
            result = await runner.run(RepoReader, QuestionInput(question=question), job_id=job_id)
            if result.status == RunStatus.SUCCESS and result.output is not None:
                print(f"\n{result.output.answer}")
                for source in result.output.sources:
                    print(f"  {source}")
                print(f"\n[{result.input_tokens} in / {result.output_tokens} out]")
            else:
                failed += 1
                print(f"\nfailed: {result.error}")
    return 1 if failed else 0


if __name__ == "__main__":
    # Words after the script are one question (`make` passes `ARGS="..."` as separate words); none means the samples.
    question = " ".join(sys.argv[1:]).strip()
    sys.exit(asyncio.run(main([question] if question else SAMPLES)))
