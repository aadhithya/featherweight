<!-- nav:top -->
[Book](../README.md) › [Part 4: Chat](../README.md#part-4-chat) › Stopping and the conversation
<!-- /nav:top -->

# Stopping and the conversation

Stop an answer midway, and keep the conversation in a file you own.

<!-- code: code/4-3/app.py -->
```python
"""Chapter 4-3: Control-C stops an answer, and the conversation is kept in a file that you own."""

import asyncio
import json
import signal
import sys
from pathlib import Path

from ag_ui.core import TextMessageContentEvent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, messages_from_dict, messages_to_dict

from featherweight import AgentRunner, ChatInput, Gemini, ModelSettings, SqliteDatabase, StreamingAgent, default_db_path

SAVED = default_db_path().with_name("chat.json")  # next to the run log: /data/chat.json in the container


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
    history: list[BaseMessage]
    if SAVED.exists():
        history = messages_from_dict(json.loads(SAVED.read_text()))
    else:
        history = [HumanMessage(Path(path).read_text()), AIMessage("I have the report. Ask me anything about it.")]
    loop = asyncio.get_running_loop()
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        while text := await read_line("\nyou> "):  # Control-C here leaves; an empty line leaves too
            user = HumanMessage(text)
            run = runner.stream(IncidentAssistant, ChatInput.from_langchain([*history, user]))
            loop.add_signal_handler(signal.SIGINT, run.cancel)  # Control-C while it answers stops the answer
            async for event in run:
                if isinstance(event, TextMessageContentEvent):
                    print(event.delta, end="", flush=True)
            loop.remove_signal_handler(signal.SIGINT)
            history += [user, *run.new_messages]  # the partial answer is in it too, if you stopped it
            SAVED.write_text(json.dumps(messages_to_dict(history)))


if __name__ == "__main__":
    try:
        asyncio.run(main(*sys.argv[1:2]))
    except (KeyboardInterrupt, EOFError):
        pass
```

```
make book-chapter N=4-3
```

Ask something, press Control-C while it answers, and ask again: the next turn knows what the assistant had already said. The next run continues from the saved conversation.

**What to notice**

- **Two meanings of Control-C.** While the agent answers, the signal handler calls `run.cancel()`, which stops the answer. While you are typing, it leaves, because the handler is only installed during a turn.
- **A stopped turn is kept.** `run.new_messages` holds the partial answer, cleaned up (a half-written tool call is dropped, and a tool call that never returned gets a "cancelled" result), so the next request can include it and the provider accepts it.
- **The run is `CANCELLED`** in the run log, as any stopped run is.
- **The history is yours.** featherweight keeps no conversation, so the example saves it to `chat.json` next to the run log, with LangChain's own converters. A database or a cache is the same idea.
- **Over the web,** a client that stops (`stop()` in `useChat`) drops the stream, and the run stops with it. To stop by id, call `runner.cancel(run_id)`, taking the id from the first event, from your own endpoint: see [stopping a response](../../streaming.md#stopping-a-response).

> **Why you keep it.** The history belongs to your application and its store, so it is not ours to keep. See
> [keeping the conversation](../../streaming.md#keeping-the-conversation).

Next: Part 5 puts the assistant on workers, with background research and traces.

<!-- nav:bottom -->
---

[← In the browser](02-in-the-browser.md) · [Part 4: Chat](../README.md#part-4-chat) · [Submit and a worker →](../05-in-production/01-submit-and-a-worker.md)
<!-- /nav:bottom -->
