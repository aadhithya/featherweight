<!-- Copied from docs/book/04-chat/01-a-streaming-agent.md by scripts/build_skill.py. Edit the original, not this file. -->

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


class IncidentAssistant(StreamingAgent):  # a streaming agent answers in a chat: no output model, no retries
    name: str = "incident_assistant"
    description: str = "Answers questions about an incident report"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "The first message is an incident report. Answer questions about it in a sentence or two, only from the report."
    )
    # the model is a setting of the agent
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
            # the conversation is yours: the whole history goes in at each turn
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

(The plain command is `docker compose run --rm --no-deps book python 4-1/app.py`, from `docs/book/code`: see [the container](https://github.com/aadhithya/featherweight/blob/main/docs/book/code/README.md).)

**What to notice**

- **A streaming agent is declared like an agent,** with no `output_model`. Nothing is validated at the end, and a failed turn is not retried: once a word is out, it cannot be taken back.
- **`runner.stream(...)` gives events,** not a result. You print each `TextMessageContentEvent.delta` as it arrives.
- **You keep the conversation.** featherweight stores no conversations: `run.new_messages` is what the agent said and did this turn, and you add it to your own history.
- **The report is the first message,** followed by a short greeting from the assistant so the roles alternate, which providers expect.
- **`read_line` reads the terminal through the event loop.** A thread stuck in `input()` would keep the process alive after Control-C.

> **Why events.** A chat has no finished output to check, so the answer is a stream a person reads as it is written. See
> [streaming agents](../../streaming.md) and [the spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/streaming.md).

## Attach a file

A message holds text, so attaching a file means putting its text in the message. Reading and attaching files is yours: you decide what a user may upload and how
it is read, and featherweight keeps no files and does no uploads. Text is the only content a message accepts: an image or a file part is refused when the
`ChatInput` is built ([the conversation is yours](../../streaming.md#the-conversation-is-yours)).

<!-- code: code/4-1/attach.py -->
```python
"""Chapter 4-1, extra: attach a file to the chat. A message holds text, so the file's text goes into the message."""

import asyncio
from pathlib import Path

from ag_ui.core import TextMessageContentEvent
from app import IncidentAssistant
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from featherweight import AgentRunner, ChatInput, SqliteDatabase, default_db_path


def attach(question: str, path: str) -> HumanMessage:
    """A user message that carries a file as extra context. Reading and attaching files is yours: featherweight takes text only."""
    file = Path(path)
    return HumanMessage(f'{question}\n\n<attachment name="{file.name}">\n{file.read_text()}\n</attachment>')


async def main(report: str = "reports/01-checkout-outage.txt", notes: str = "4-1/runbook.md") -> None:
    greeting = AIMessage("I have the report. Ask me anything about it.")
    history: list[BaseMessage] = [HumanMessage(Path(report).read_text()), greeting]
    question = attach("Does the runbook say what to do about this?", notes)  # the question, with the file attached
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        async for event in runner.stream(IncidentAssistant, ChatInput.from_langchain([*history, question])):
            if isinstance(event, TextMessageContentEvent):
                print(event.delta, end="", flush=True)
    print()


if __name__ == "__main__":
    asyncio.run(main())
```

Run it with `docker compose run --rm --no-deps book python 4-1/attach.py`, from `docs/book/code`. The file is `4-1/runbook.md`. Something like:

```text
Yes. Roll back the last release of the payment form, and tell the payments team.
```

- **The file is labelled** with its name, so the model can tell the report from the attachment.
- **Binary files and images** are yours to turn into text first (for example by extracting the text of a PDF): featherweight does not convert them.

Next: the same agent, for a browser.
