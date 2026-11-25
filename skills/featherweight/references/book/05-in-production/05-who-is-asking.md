<!-- Copied from docs/book/05-in-production/05-who-is-asking.md by scripts/build_skill.py. Edit the original, not this file. -->

# Who is asking

A tool that acts for the person who asked, and checks first.

Pass the identity of the run, and read it in a tool.

<!-- code: code/5-5/app.py -->
```python
"""Chapter 5-5: who is asking. A tool that checks the identity of the run before it acts."""

import asyncio
import sys
from typing import ClassVar

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel

from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, RunContext, SqliteDatabase

INCIDENTS = {"INC-7": ("payments", "Checkout is down for card payments."), "INC-9": ("search", "Search is slow.")}


class Responder(BaseModel):  # who the run is for: you define it, small, and no secrets
    user_id: str
    team: str


@tool
async def open_incident(incident_id: str, runtime: ToolRuntime[RunContext[Responder]]) -> str:
    """Open an incident by its id."""
    who = runtime.context.identity  # the model never sees this parameter, and cannot change it
    team, summary = INCIDENTS.get(incident_id, ("", ""))
    if who is None or team != who.team:  # featherweight carries the identity; checking it is the tool's job
        return "Not allowed."
    return summary


class Question(AgentInput):
    text: str


class Answer(AgentOutput):
    answer: str | None = None


class IncidentLookup(AgentBase):
    name: str = "incident_lookup"
    description: str = "Answers a question from the incidents the asker may open"
    version: str = "1"
    prompt: str = "Answer from the incidents you can open with your tool. If you cannot open one, say so."
    tools: list = [open_incident]
    input_model: ClassVar[type[AgentInput]] = Question
    output_model: ClassVar[type[AgentOutput]] = Answer


async def main(user_id: str = "ana", team: str = "payments") -> None:
    async with SqliteDatabase(":memory:") as db:
        runner = AgentRunner(db, identity_type=Responder)  # a declared type makes identity= required on every run
        result = await runner.run(
            IncidentLookup,
            Question(text="What is going on with INC-7?"),
            identity=Responder(user_id=user_id, team=team),
        )
        print(result.output.answer if result.output else result.error)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
```

Run it as `ana` (the payments team) and as `raj` (search): `python app.py ana payments`, `python app.py raj search`. The tool opens `INC-7` for ana. For raj it
returns `Not allowed.`, and the model says it could not open it.

**What to notice**

- **The identity is a model you define,** declared once on the runner (`AgentRunner(db, identity_type=Responder)`) and passed with `identity=`. A declared type makes `identity=` required, so one code path
  that forgets it cannot run a tool as nobody; `Responder | None` allows runs without one.
- **A tool reads it from its `RunContext`.** The model never sees the parameter and cannot set it. If one agent serves different kinds of people (`identity_type=Teacher | Student`), the tool checks which kind it got before it acts. A launched agent and an agent started in the background have the identity of their parent.
- **featherweight carries it; you decide.** Checking what the identity may do is the tool's job, and a refusal is text, as any failure of a tool is.
- **On a worker it is the same.** `client.submit(job, input, identity=...)` puts it in the message with its kind, the name of its class, and `JobWorker(..., identity_type=Responder)` rebuilds it as that model ([the guide](../../submitted-jobs.md#who-the-job-is-for-the-identity)). The runner must accept the worker's type, and a rename of the class while a job is queued fails that job.
- **It is a snapshot,** taken when the job was submitted. A queued job can run later, after a role changed or a token expired: for a sensitive action, the tool re-checks with your application by user id.
  A tool gets a credential from the identity with your own code, at each call. The identity is small and has no secrets in it: it sits in the queue message.
- **A job is for one identity.** If you pass a `job_id` to `run` yourself, pass the same identity every time: a run for another one raises. Resuming makes a new job, and reuses a step only for the same identity; whether someone may resume a job at all is your check.
- **A field that is not sent** (`exclude=True`, for a value that exists only in your process) needs a default, because the worker rebuilds the identity without it. Do not change an identity after a run has it.
- **If you build agents from what users write,** do not let them declare stdio MCP servers: they run commands on your server. Names of jobs are yours: unique in what your lookup searches, and stable.

> **Why carry and not enforce.** Authentication, what a user may do and where credentials live differ in every application, so featherweight hands a typed identity to the tools and leaves the rest to you. See [identity](https://github.com/aadhithya/featherweight/blob/main/docs/specs/identity.md) and [the guide](../../agents.md#who-is-asking-the-identity-of-a-run).

**Also possible**

- [A tool that gets a token for its identity](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-tool-that-gets-a-token-for-its-identity.md).
- Files are passed as references, not carried: [files for an agent or a job](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/files-for-an-agent-or-a-job.md).

Next: bring your own storage.
