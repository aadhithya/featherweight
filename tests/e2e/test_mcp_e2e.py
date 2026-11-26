"""End-to-end test of an agent with MCP tools, with the real model: it calls a tool over MCP, reads a picture a tool
returns, and reads a PDF a tool returns. The server is the fake one of the unit tests. About four model calls for each test and provider. Run with
`make e2e ARGS="-k mcp"`. Not part of `make test`.
"""

import logging
import re
import sys
from pathlib import Path
from typing import ClassVar

from featherweight.mcp import StdioMCPServer
from featherweight.models import AgentBase, AgentInput, AgentOutput, Status

SERVER = StdioMCPServer(
    name="fake",
    command=sys.executable,
    args=(str(Path(__file__).parent.parent / "mcp_fake_server.py"),),
    tools=("add", "picture", "access_note"),
    discover=False,  # bound directly
)
FOUND = SERVER.model_copy(
    update={"discover": True, "description": "Small tools: add numbers, show a picture, read documents"}
)  # found by search


def tools_given_to_the_model(caplog) -> list[str]:
    """The tools that were bound for the one attempt, from the runner's `attempt_started` line (the run log keeps a hash)."""
    [tools] = re.findall(r"attempt_started .* tools=(\S*)", "\n".join(r.getMessage() for r in caplog.records))
    return sorted(tools.split(","))


class TaskInput(AgentInput):
    task: str


class TaskOutput(AgentOutput):
    sum: int | None = None
    colour: str | None = None
    access_code: str | None = None


class Helper(AgentBase):
    name: str = "helper"
    description: str = "Answers with the help of its tools"
    version: str = "0.1.0"
    prompt: str = "Do what the task says, using your tools, and report the results."
    mcp_servers: list[StdioMCPServer] = [SERVER]
    input_model: ClassVar[type[AgentInput]] = TaskInput
    output_model: ClassVar[type[AgentOutput]] = TaskOutput


class Seeker(Helper):
    name: str = "seeker"
    mcp_servers: list[StdioMCPServer] = [FOUND]


async def test_an_agent_finds_mcp_tools_by_search_and_calls_them(db, runner, provider, caplog):
    caplog.set_level(logging.INFO, logger="featherweight")
    task = "Add 17 and 25. Read the document from the access note tool and report the access code."
    job_id = await db.create_job("mcp-discovery-e2e")
    result = await runner.run(provider.on(Seeker), TaskInput(task=task), job_id=job_id)

    assert result.status == Status.SUCCESS, result.error
    out = result.output
    print(f"\n[output] {out}\n[tokens] in={result.input_tokens} out={result.output_tokens}")
    assert out.sum == 42 and out.access_code and "7421" in out.access_code
    assert tools_given_to_the_model(caplog) == ["call_tool", "search_tools"]  # the server's tools were never bound


async def test_an_agent_uses_mcp_tools_and_reads_what_they_return(db, runner, provider, caplog):
    caplog.set_level(logging.INFO, logger="featherweight")
    task = (
        "Add 17 and 25 with the add tool. Look at the picture from the picture tool and name its single colour. "
        "Read the document from the access_note tool and report the access code."
    )
    job_id = await db.create_job("mcp-e2e")
    # no `async with`: it is optional
    result = await runner.run(provider.on(Helper), TaskInput(task=task), job_id=job_id)

    assert result.status == Status.SUCCESS, result.error
    out = result.output
    print(f"\n[output] {out}\n[tokens] in={result.input_tokens} out={result.output_tokens}")
    assert out.sum == 42
    assert out.colour and "red" in out.colour.lower()
    assert out.access_code and "7421" in out.access_code
    assert tools_given_to_the_model(caplog) == ["fake_access_note", "fake_add", "fake_picture"]
