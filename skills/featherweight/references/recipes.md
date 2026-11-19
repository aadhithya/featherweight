# Recipes

Patterns for building with `featherweight`. Each snippet is complete enough to adapt. The agent definitions are in `SKILL.md`.

## A pipeline of two agents

Order and branching are plain Python. Both runs share a job, so they share the run store and the trace.

```python
import featherweight as ftw


async def pipeline(runner, job_id, texts):
    results = await runner.run_many(PeopleExtractor, [ExtractInput(text=t) for t in texts], job_id=job_id)
    people = []
    for result in results:
        if result.status == RunStatus.SUCCESS:  # decide here what a failed run means: skip, stop, ask again
            people.extend(result.output.people)
    return sorted(set(people))


async def main():
    async with Database("featherweight.db") as db:
        runner = AgentRunner(db)
        outcome = await ftw.run_job(
            db, "people", lambda job_id: pipeline(runner, job_id, ["Anna met Tom.", "Li came."])
        )
        # outcome.status, outcome.result on success, outcome.error on failure
```

`run_job` creates the job, runs your function and records `SUCCESS` or `FAILED`. A pipeline that raises comes back as a
failed outcome, not an exception.

## Resuming a failed job

Run the same pipeline again, and point it at the failed job:

```python
outcome = await run_job(db, "my_pipeline", lambda job_id: my_pipeline(runner, job_id), resume_from=failed.job_id)
```

Agent runs that succeeded with the same agent and input are answered from the log (`result.reused`, zero tokens). Keep
the code between agents deterministic (no clock, no `random`), and do not rely on the run store for a resume.

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

No test makes a real model call. A fake is **model settings**, like a real provider: a `ModelSettings` subclass whose
`build()` returns a fake chat model. The test runs the same agent with its `model` replaced by the fake settings, and the
runner takes the path it takes in production, `agent.model.build()`. The structured reply is a tool call named after the
output class. This is a whole test file for the agent in `SKILL.md` (saved as `app.py`). Tests need
`asyncio_mode = auto` (in `pyproject.toml`, `[tool.pytest.ini_options]`) or `pytest -o asyncio_mode=auto`, and
`pytest-asyncio`.

```python
from typing import Any, ClassVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app import ExtractInput, PeopleExtractor  # your agent module
import featherweight as ftw


class FakeSettings(ftw.ModelSettings):
    """Model settings that build the fake chat model they hold. Any provider's settings work the same way."""

    provider: ClassVar[str] = "fake"
    model: str = "fake-model"
    chat: Any = Field(default=None, exclude=True)  # the fake chat model; not part of the settings' data

    def build(self):
        return self.chat


class FakeModel(BaseChatModel):
    """Replies by what the input contains, so `run_many` gives each input its own reply. `answers` maps a piece of
    input text to the outputs to give, one for each call; the last one repeats."""

    answers: dict[str, list[dict]]
    calls: dict[str, int] = {}
    total: int = 0  # model calls

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = str(next(m.content for m in messages if m.type == "human"))
        key = next(k for k in self.answers if k in user)
        n = self.calls.get(key, 0)
        self.calls[key] = n + 1
        self.total += 1
        output = self.answers[key][min(n, len(self.answers[key]) - 1)]
        call = {"name": "ExtractOutput", "args": output, "id": f"call-{self.total}"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


async def run(tmp_path, answers, text="Anna came."):
    fake = FakeModel(answers=answers)  # one fake for all attempts, so its counters last

    class UnderTest(PeopleExtractor):  # the same agent, run by the fake
        model: ftw.ModelSettings = Field(default_factory=lambda: FakeSettings(chat=fake))

    async with ftw.Database(tmp_path / "test.db") as db:
        runner = ftw.AgentRunner(db, retry_delay_s=0)
        return await runner.run(UnderTest, ExtractInput(text=text)), fake


async def test_success(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Anna"]}]})
    assert result.status == ftw.RunStatus.SUCCESS and result.output.people == ["Anna"]


async def test_check_output_rejects_and_the_runner_retries(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Nobody"]}, {"people": ["Anna"]}]})
    assert result.status == ftw.RunStatus.SUCCESS and result.attempts == 2


async def test_a_run_that_keeps_failing_is_returned_as_failed(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Nobody"]}]})  # the last reply repeats
    assert result.status == ftw.RunStatus.FAILED and result.attempts == 3  # max_attempts
    assert "names not in the text" in (result.error or "")


async def test_a_reply_that_breaks_the_schema_is_fixed_inside_the_attempt(tmp_path):
    result, fake = await run(tmp_path, {"Anna": [{"people": "not a list"}, {"people": ["Anna"]}]})
    assert result.status == ftw.RunStatus.SUCCESS and result.attempts == 1 and fake.total == 2
```

A schema violation (a wrong type, a value outside a `Literal`) is sent back to the model, which gets a second chance in
the same attempt. A rejection by `check_output` fails the attempt, and the runner retries it, up to `max_attempts`. Use
a `Literal` or a validator for rules on the output alone, and `check_output` for rules between the input and the output.

## An agent with an MCP server

```python
import featherweight as ftw


class RepoReader(AgentBase):
    name: str = "repo_reader"
    description: str = "Answers questions about a GitHub repository"
    version: str = "0.1.0"
    prompt: str = "Answer from the repository, using the tools."
    mcp_servers: list = [
        ftw.HttpMCPServer(
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

## A chat agent that streams

```python
from contextlib import aclosing

import featherweight as ftw


class Support(ftw.StreamingAgent):
    name: str = "support"
    description: str = "Answers customers"
    version: str = "1"
    prompt: str = "You help customers. Be short."
    model: ModelSettings = ftw.Claude(model="claude-haiku-5-5", effort="low", thinking="off")


run = runner.stream(Support, ftw.ChatInput.from_langchain([*history.messages, user_message]))
try:
    async with aclosing(ftw.agui.encode(run)) as pieces:  # SSE strings: send them with any web framework
        async for piece in pieces:
            yield piece
finally:
    history.add_messages([user_message, *run.new_messages])  # you keep the conversation; featherweight keeps none
```

`runner.cancel(run.run_id)` stops it. `featherweight.adapters.vercel` does the same for `useChat`. A chat in the terminal is
`examples/chat/cli.py`, and the same agent behind FastAPI is `examples/chat/api.py`; `references/streaming.md` has the details. Test it with a fake streaming chat model: the
`Streams` double in the repository's `tests/fakes.py` shows how.

## A different provider

```python
import featherweight as ftw


class Triager(AgentBase):
    ...
    model: ftw.Claude = ftw.Claude(model="claude-haiku-5-5", effort="low")
```

`pip install "featherweight[anthropic]"` and set `ANTHROPIC_API_KEY`.
