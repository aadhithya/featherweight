<!-- nav:top -->
[Book](../README.md) › [Part 4: Chat](../README.md#part-4-chat) › A streaming agent
<!-- /nav:top -->

# A streaming agent

Turn the assistant into a chat that streams its answer.

This part adds a chat to the assistant from Part 1. You can read it without the other parts.

<!-- code: code/4-1/app.py -->
```python
"""Chapter 4-1: a chat that streams its answer. The report is the first message of the conversation."""

import asyncio
import sys
from pathlib import Path

from ag_ui.core import TextMessageContentEvent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from featherweight import AgentRunner, ChatInput, Gemini, ModelSettings, SqliteDatabase, StreamingAgent, default_db_path


class IncidentAssistant(StreamingAgent):
    name: str = "incident_assistant"
    description: str = "Answers questions about an incident report"
    version: str = "1"
    prompt: str = (
        "The first message is an incident report. Answer questions about it in a sentence or two, only from the report."
    )
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")


async def read_line(prompt: str) -> str:  # the event loop reads the terminal, so that Control-C can leave
    loop = asyncio.get_running_loop()
    line: asyncio.Future[str] = loop.create_future()
    print(prompt, end="", flush=True)
    loop.add_reader(sys.stdin, lambda: line.set_result(sys.stdin.readline().strip()))
    try:
        return await line
    finally:
        loop.remove_reader(sys.stdin)


async def main(path: str = "reports/01-checkout-outage.txt") -> None:
    greeting = "I have the report. Ask me anything about it."
    history: list[BaseMessage] = [HumanMessage(Path(path).read_text()), AIMessage(greeting)]  # featherweight keeps none
    print(greeting)
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        while text := await read_line("\nyou> "):  # an empty line leaves
            user = HumanMessage(text)
            run = runner.stream(IncidentAssistant, ChatInput.from_langchain([*history, user]))
            async for event in run:
                if isinstance(event, TextMessageContentEvent):
                    print(event.delta, end="", flush=True)  # the answer, as the model writes it
            history += [user, *run.new_messages]  # what was said, for the next turn


if __name__ == "__main__":
    try:
        asyncio.run(main(*sys.argv[1:2]))
    except (KeyboardInterrupt, EOFError):
        pass
```

Run it, and type a question about the report. An empty line, or Control-C, leaves.

```
make book-chapter N=4-1
```

```
I have the report. Ask me anything about it.

you> why did checkout fail?
A new release of the payment form sent a field the provider rejects, so every payment failed.
```

(The plain command is `docker compose run --rm --no-deps book python 4-1/app.py`, from `docs/book/code`: see [the container](../code/README.md).)

**What to notice**

- **A streaming agent is declared like an agent,** with no `output_model`. Nothing is validated at the end, and a failed turn is not retried: once a word is out, it cannot be taken back.
- **`runner.stream(...)` gives events,** not a result. You print each `TextMessageContentEvent.delta` as it arrives.
- **You keep the conversation.** featherweight stores no conversations: `run.new_messages` is what the agent said and did this turn, and you add it to your own history.
- **The report is the first message,** followed by a short greeting from the assistant so the roles alternate, which providers expect.
- **`read_line` reads the terminal through the event loop.** A thread stuck in `input()` would keep the process alive after Control-C.

> **Why events.** A chat has no finished output to check, so the answer is a stream a person reads as it is written. See
> [streaming agents](../../streaming.md) and [the spec](../../specs/streaming.md).

Next: the same agent, for a browser.

<!-- nav:bottom -->
---

[← Resume](../03-jobs-and-pipelines/04-resume.md) · [Part 4: Chat](../README.md#part-4-chat) · [In the browser →](02-in-the-browser.md)
<!-- /nav:bottom -->
