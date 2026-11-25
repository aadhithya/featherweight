# Files for an agent or a job: pass a reference

A chat takes [attachments](../streaming.md#attachments). An agent that a job runs, and a job in a queue, **do not carry files, on purpose**. A file does not travel from one
agent to another: not to an agent that another agent launched, not to a background job, not through a queue. What travels is a **reference**: an id or a name for a
file that you keep in a store of your own (a database, a disk, S3). The input says which file, the identity says who may read it, and a tool does the fetch and the
check.

```python
import base64

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel

from featherweight import RunContext


class Teacher(BaseModel):  # who the run is for: see the identity recipe
    user_id: str


def document_tools(files, directory):
    """`files` and `directory` are yours: where the files live, and who may read which."""

    @tool
    async def read_document(document_id: str, runtime: ToolRuntime[RunContext[Teacher]]) -> str:
        """Read a document by its reference, and return its text."""
        who = runtime.context.identity
        if who is None or not await directory.may_read(who.user_id, document_id):  # the model chose the id: check it
            return "Not allowed."
        return await files.text(document_id)  # text is what every provider takes

    @tool
    async def show_document(document_id: str, runtime: ToolRuntime[RunContext[Teacher]]) -> str | list[dict]:
        """Look at a scanned document (an image or a PDF) by its reference."""
        who = runtime.context.identity
        if who is None or not await directory.may_read(who.user_id, document_id):
            return "Not allowed."
        data, mime_type = await files.read(document_id)
        kind = "image" if mime_type.startswith("image/") else "file"
        return [  # content blocks: the model reads the file itself, where the provider accepts it
            {"type": "text", "text": f"The document {document_id}."},
            {"type": kind, "base64": base64.b64encode(data).decode(), "mime_type": mime_type},
        ]

    return read_document, show_document
```

The job's input and the agent's input hold the reference, and never the file. A pipeline that fetches a file puts it in the store first, and passes the reference on:

```python
from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    JobContext,
    ModelSettings,
    job,
    run_job,
)


class ReportInput(AgentInput):
    document_id: str  # a reference: where the file is, and not the file


class ReportOutput(AgentOutput):
    summary: str


class Summariser(AgentBase):
    name: str = "summariser"
    description: str = "Summarises one document"
    version: str = "1"
    prompt: str = "Read the document that the input points to, with a tool, and summarise it in two sentences."
    model: ModelSettings = Gemini()
    tools: list = [*document_tools(files, directory)]
    input_model = ReportInput
    output_model = ReportOutput


class Download(BaseModel):  # what the job is given: a reference and where to fetch it from
    document_id: str
    url: str


@job
async def summarise_a_download(ctx: JobContext, input: Download) -> ReportOutput:
    await files.put(
        input.document_id, await download(input.url)
    )  # your code fetches the file and keeps it: featherweight fetches nothing
    result = await ctx.run(Summariser, ReportInput(document_id=input.document_id))  # the agent gets the reference
    return result.output


runner = AgentRunner(db, identity_type=Teacher)
outcome = await run_job(
    runner, summarise_a_download, Download(document_id="doc-7", url="https://..."), identity=Teacher(user_id="u1")
)
```

- **Why no files between agents.** The input of an agent that another agent launches is JSON that a model writes, and a model cannot write bytes. The input of a job goes
  through a queue, the job store, the input hash and the traces, and a file would go through all of them. The run log keeps no content. The same holds in the other
  frameworks: a sub-agent gets text, or a reference to something shared.
- **The model sees the reference and can pass another one.** The tool checks that the identity may read the one it was given, as in the identity recipe. A tool that
  checks nothing gives every file to whoever asks.
- **Text is the portable result of a tool.** An image or a PDF block reaches the model as it is. It was **read** on 2026-11-25 by the smallest model of Anthropic, Google
  (Gemini) and OpenAI (on the Responses API), with one small file each; Gemma did not call the tool, so nothing is known for it. That is a snapshot and not a promise:
  try your provider and your file type.
- **A resume reuses a step for the same reference**, even if the file behind it changed. Put a version or a hash in the reference when that matters (`doc-7@3`).
- See [attachments](../specs/attachments.md#files-for-agents-and-jobs) for the rule, and [a job that a worker runs](a-job-that-a-worker-runs.md) for the queue.
