# Testing with a fake model

No test makes a real model call. A fake is **model settings**, like a real provider: a `ModelSettings` subclass whose
`build()` returns a fake chat model. The test runs the same agent with its `model` replaced by the fake settings, and the
runner takes the path it takes in production, `agent.model.build()`. The structured reply is a tool call named after the
output class. This is a whole test file for the agent of [Getting started](../getting-started.md) (saved as `app.py`). Tests need
`asyncio_mode = auto` (in `pyproject.toml`, `[tool.pytest.ini_options]`) or `pytest -o asyncio_mode=auto`, and
`pytest-asyncio`.

```python
from typing import Any, ClassVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app import ExtractInput, PeopleExtractor  # your agent module
from featherweight import AgentRunner, ModelSettings, Status, SqliteDatabase


class FakeSettings(ModelSettings):
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
        model: ModelSettings = Field(default_factory=lambda: FakeSettings(chat=fake))

    async with SqliteDatabase(tmp_path / "test.db") as db:
        runner = AgentRunner(db, retry_delay_s=0)
        return await runner.run(UnderTest, ExtractInput(text=text)), fake


async def test_success(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Anna"]}]})
    assert result.status == Status.SUCCESS and result.output.people == ["Anna"]


async def test_check_output_rejects_and_the_runner_retries(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Nobody"]}, {"people": ["Anna"]}]})
    assert result.status == Status.SUCCESS and result.attempts == 2


async def test_a_run_that_keeps_failing_is_returned_as_failed(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Nobody"]}]})  # the last reply repeats
    assert result.status == Status.FAILED and result.attempts == 3  # max_attempts
    assert "names not in the text" in (result.error or "")


async def test_a_reply_that_breaks_the_schema_is_fixed_inside_the_attempt(tmp_path):
    result, fake = await run(tmp_path, {"Anna": [{"people": "not a list"}, {"people": ["Anna"]}]})
    assert result.status == Status.SUCCESS and result.attempts == 1 and fake.total == 2
```

A schema violation (a wrong type, a value outside a `Literal`) is sent back to the model, which gets a second chance in
the same attempt. A rejection by `check_output` fails the attempt, and the runner retries it, up to `max_attempts`. Use
a `Literal` or a validator for rules on the output alone, and `check_output` for rules between the input and the output.
