"""End-to-end test of an agent with MCP tools, with the real model: it calls a tool over MCP, reads a picture a tool
returns, and reads a PDF a tool returns. The server is the fake one of the unit tests. About four model calls. Run with
`make e2e ARGS="-k mcp"` (needs GOOGLE_API_KEY in .env). Not part of `make test`.
"""

import sys
from pathlib import Path
from typing import ClassVar

from micro.mcp import StdioMCPServer
from micro.models import AgentBase, AgentInput, AgentOutput, RunStatus
from micro.runner import AgentRunner

SERVER = StdioMCPServer(
    name="fake",
    command=sys.executable,
    args=(str(Path(__file__).parent.parent / "mcp_fake_server.py"),),
    tools=("add", "picture", "access_note"),
)


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
    model_name: str = "gemini-3.5-flash-lite"
    reasoning: str = "low"
    mcp_servers: list[StdioMCPServer] = [SERVER]
    input_model: ClassVar[type[AgentInput]] = TaskInput
    output_model: ClassVar[type[AgentOutput]] = TaskOutput


async def test_an_agent_uses_mcp_tools_and_reads_what_they_return(db):
    task = (
        "Add 17 and 25 with the add tool. Look at the picture from the picture tool and name its single colour. "
        "Read the document from the access_note tool and report the access code."
    )
    job_id = await db.create_job("mcp-e2e")
    result = await AgentRunner(db).run(Helper, TaskInput(task=task), job_id=job_id)  # no `async with`: it is optional

    assert result.status == RunStatus.SUCCESS, result.error
    out = result.output
    print(f"\n[output] {out}\n[tokens] in={result.input_tokens} out={result.output_tokens}")
    assert out.sum == 42
    assert out.colour and "red" in out.colour.lower()
    assert out.access_code and "7421" in out.access_code
    (row,) = await db.list_runs(job_id=job_id)
    assert sorted(row.tool_names) == ["fake_access_note", "fake_add", "fake_picture"]
