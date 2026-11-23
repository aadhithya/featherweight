<!-- nav:top -->
[Book](../README.md) › [Part 5: In production](../README.md#part-5-in-production) › Background jobs  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# Background jobs

Let an agent start another agent and go on.

Let the chat start a researcher, and carry on talking while it works.

The chat is the assistant from Part 4, with one addition: a list of agents it may start in the background.

```python
class IncidentAssistant(StreamingAgent):
    ...
    background_jobs: list[type] = [RootCauseResearcher]  # in assistant.py
```

The `Session` is **yours**: featherweight keeps no conversation, so a small class holds the history and wakes the chat when the researcher is done.

<!-- code: code/5-3/app.py -->
```python
"""Chapter 5-3: a chat that starts a researcher in the background, and a session that wakes it when the note is ready."""

import asyncio
import sys
from pathlib import Path

from ag_ui.core import CustomEvent, TextMessageContentEvent
from assistant import IncidentAssistant
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from terminal import read_line

from featherweight import AgentRunner, ChatInput, Database, SqliteDatabase, job_events


class Session:
    """The conversation, and the wake-up. featherweight keeps neither: this is yours to copy and change."""

    def __init__(self, db: Database, runner: AgentRunner, history: list[BaseMessage]):
        self.db, self.runner, self.history = db, runner, history
        self.turn = asyncio.Lock()  # one turn at a time
        self.watching: set[asyncio.Task] = set()

    async def say(self, message: BaseMessage) -> None:
        async with self.turn:
            # the conversation is yours: the whole history goes in at each turn
            run = self.runner.stream(IncidentAssistant, ChatInput.from_langchain([*self.history, message]))
            async for event in run:
                if isinstance(event, TextMessageContentEvent):
                    print(event.delta, end="", flush=True)
                elif isinstance(event, CustomEvent) and event.name == "background_started":
                    task = asyncio.create_task(self.wake(event.value["job_id"]))  # follow the child
                    self.watching.add(task)
                    task.add_done_callback(self.watching.discard)
            print()
            self.history += [message, *run.new_messages]

    async def wake(self, job_id: str) -> None:
        async for _ in job_events(self.db, job_id):  # until the child's job is final
            pass
        print("\n[the researcher is done]")
        await self.say(await self.runner.background_message(job_id))  # the next turn, with its result


async def main(path: str = "reports/01-checkout-outage.txt") -> None:
    report = HumanMessage(Path(path).read_text())
    async with (
        SqliteDatabase(":memory:") as db,
        AgentRunner(db) as runner,
    ):  # in memory: a restart loses what is pending
        session = Session(db, runner, [report, AIMessage("I have the report. Ask me anything about it.")])
        while text := await read_line("\nyou> "):  # try: what was the root cause?
            await session.say(HumanMessage(text))
        for task in session.watching:
            task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main(*sys.argv[1:2]))
    except (KeyboardInterrupt, EOFError):
        pass
```

```text
you> what was the root cause?
I started the researcher; its note will come.
[the researcher is done]
The researcher says: something like the cause named in the note.
```

**What to notice**

- **`background_jobs`** gives the model one more tool, `start_background`. It returns a job id at once, so the turn goes on. The researcher is an ordinary agent, run as a job of its own.
- **Results need a result store.** The runner reads the researcher's note from it (the default sqlite store is enough here). An agent with `background_jobs` and no result store is refused before any model call.
- **The stream says what happened.** The chat's stream carries `background_started` (with the child's `job_id`), which is how the session knows what to follow, and `background_finished`.
  The child's own job events are the full record: `job_events` follows them until the job is final.
- **`runner.background_message(job_id)`** builds the user message with the child's result, and the session starts the next turn with it. A chat turn does not wait for its children.
- **Here or on a worker** is one line: `AgentRunner(db, background=client)` submits the researcher to a queue, and a worker that lists its job runs it.
- **Nothing is kept for you.** If the process restarts, the history and the notes that were not stored are lost; this session keeps both in memory.

> **Why a message, not a tool result.** The tool call already returned ("started"), and providers reject a second result for it. The result arrives as new information, as a user message.
> See [background jobs](../../specs/background-agents.md#getting-the-result-back) and [the guide](../../background-agents.md).

Next: see what all of it did.

<!-- nav:bottom -->
---

[← Several workers](02-several-workers.md) · [Part 5: In production](../README.md#part-5-in-production) · [Observability →](04-observability.md)
<!-- /nav:bottom -->
