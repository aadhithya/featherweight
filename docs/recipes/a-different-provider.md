# A different provider

Run an agent on another model or provider by changing one line: the `model` of the agent. Each provider has a settings class that checks its own options when the agent is built.

```python
import asyncio
from typing import ClassVar

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Claude,  # or Gemini, or OpenAI
    ModelSettings,
    SqliteDatabase,
    default_db_path,
)


class Ticket(AgentInput):
    text: str


class Triage(AgentOutput):
    urgency: str = "low"


class Triager(AgentBase):
    name: str = "triager"
    description: str = "Rates how urgent a ticket is"
    version: str = "0.1.0"
    prompt: str = "Rate how urgent the ticket is: low, medium or high."
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low")  # the one line to change
    input_model: ClassVar[type[AgentInput]] = Ticket
    output_model: ClassVar[type[AgentOutput]] = Triage


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        result = await runner.run(Triager, Ticket(text="The checkout page is down for everyone."))
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

`pip install "featherweight[anthropic]"` and set `ANTHROPIC_API_KEY` (`openai` and `GOOGLE_API_KEY` for the others). A model that is not in a class's list, or a provider that has no class, is a few lines: [add a model or a provider](../models.md#add-a-model-gpt-5x).
