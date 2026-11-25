# An agent that launches another

Hand part of the work to another agent. The editor below asks a summariser for a summary, then writes the headline. It can launch only the agents on its list.

```python
import asyncio
from typing import ClassVar

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    default_db_path,
)


class Topic(AgentInput):
    topic: str


class Summary(AgentOutput):
    summary: str = ""


class Headline(AgentOutput):
    headline: str = ""


class Summariser(AgentBase):
    name: str = "summariser"
    description: str = "Summarises a topic in one sentence"  # the editor's model reads this
    version: str = "0.1.0"
    prompt: str = "Summarise the topic in one sentence."
    model: ModelSettings = Gemini()
    input_model: ClassVar[type[AgentInput]] = Topic
    output_model: ClassVar[type[AgentOutput]] = Summary


class Editor(AgentBase):
    name: str = "editor"
    description: str = "Writes a headline"
    version: str = "0.1.0"
    prompt: str = "Write a headline. Ask the summariser for a summary first."
    model: ModelSettings = Gemini()
    subagents: list[type] = [Summariser]  # classes: the only agents this one may launch
    input_model: ClassVar[type[AgentInput]] = Topic
    output_model: ClassVar[type[AgentOutput]] = Headline


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        result = await runner.run(Editor, Topic(topic="a new bridge"))
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

The model sees only the list you wrote, and `max_depth` and `max_launches` limit how far it goes. See [launching agents](../launching-agents.md).
