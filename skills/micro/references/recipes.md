# Recipes

Patterns for building with `micro`. Each snippet is complete enough to adapt. The agent definitions are in `SKILL.md`.

## A pipeline of two agents

Order and branching are plain Python. Both runs share a job, so they share the run store and the trace.

```python
from micro.jobs import run_job


async def pipeline(runner, job_id, texts):
    results = await runner.run_many(PeopleExtractor, [ExtractInput(text=t) for t in texts], job_id=job_id)
    people = []
    for result in results:
        if result.status == RunStatus.SUCCESS:  # decide here what a failed run means: skip, stop, ask again
            people.extend(result.output.people)
    return sorted(set(people))


async def main():
    async with Database("micro.db") as db:
        runner = AgentRunner(db)
        outcome = await run_job(db, "people", lambda job_id: pipeline(runner, job_id, ["Anna met Tom.", "Li came."]))
        # outcome.status, outcome.result on success, outcome.error on failure
```

`run_job` creates the job, runs your function and records `SUCCESS` or `FAILED`. A pipeline that raises comes back as a
failed outcome, not an exception.

## Telling a retry what went wrong

A retry does not see the last error. If the next attempt should, make it part of the input:

```python
class FillInput(AgentInput):
    form: str
    problems: list[str] = []


result = await runner.run(Filler, FillInput(form=form), job_id=job_id)
for _ in range(2):
    if result.status == RunStatus.SUCCESS:
        break
    result = await runner.run(Filler, FillInput(form=form, problems=[result.error or ""]), job_id=job_id)
```

## Testing with a fake model

No test makes a real model call. Give the runner a chat model that replays a script. The structured reply is a tool call
named after the output class.

```python
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class Scripted(BaseChatModel):
    script: list[AIMessage]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = self.script[self.calls]
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def reply(**output):
    call = {"name": "ExtractOutput", "args": output, "id": "call-1"}
    return AIMessage(content="", tool_calls=[call])


async def test_extracts_people(tmp_path):
    async with Database(tmp_path / "t.db") as db:
        fake = Scripted(script=[reply(people=["Anna Berger"])])
        runner = AgentRunner(db, model_factory=lambda agent: fake, retry_delay_s=0)
        result = await runner.run(PeopleExtractor, ExtractInput(text="Anna Berger spoke."))
        assert result.status == RunStatus.SUCCESS and result.output.people == ["Anna Berger"]
```

A `FAILED` outcome is tested the same way: script a reply that `check_output` rejects, and give `max_attempts=1`.

## An agent with an MCP server

```python
from micro.mcp import HttpMCPServer


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

## An agent that launches another

```python
class Editor(AgentBase):
    name: str = "editor"
    description: str = "Writes a headline"
    version: str = "0.1.0"
    prompt: str = "Write a headline. Ask the summariser for a summary first."
    subagents: list[type] = [Summariser]  # classes: the only agents this one may launch
```

## A different provider

```python
from micro.model_settings import Claude


class Triager(AgentBase):
    ...
    model: Claude = Claude(model="claude-haiku-5-5", effort="low")
```

`pip install "micro[anthropic] @ git+https://github.com/aadhithya/micro"` and set `ANTHROPIC_API_KEY`.
