# A streaming agent for chat

Stream a chat answer to a browser, as server-sent events, and keep the conversation yourself. featherweight keeps none: you give each turn the whole history, and you store what the turn produced.

```python
import asyncio
from contextlib import aclosing

from langchain_core.messages import BaseMessage, HumanMessage

from featherweight import AgentRunner, ChatInput, Claude, ModelSettings, SqliteDatabase, StreamingAgent, default_db_path
from featherweight.adapters import agui


class Support(StreamingAgent):
    name: str = "support"
    description: str = "Answers customers"
    version: str = "1"
    prompt: str = "You help customers. Be short."
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low", thinking="off")


history: list[BaseMessage] = []  # yours: keep the conversation where you like (a database, a file)


async def answer(runner: AgentRunner, user_message: HumanMessage):
    """The server-sent events of one turn: send them with any web framework."""
    run = runner.stream(Support, ChatInput.from_langchain([*history, user_message]))
    try:
        async with aclosing(agui.encode(run)) as pieces:
            async for piece in pieces:
                yield piece
    finally:
        history.extend([user_message, *run.new_messages])  # what the turn produced, for the next turn


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        async for piece in answer(runner, HumanMessage("Where is my order?")):
            print(piece, end="")


if __name__ == "__main__":
    asyncio.run(main())
```

`runner.cancel(run.run_id)` stops a turn. `featherweight.adapters.vercel` does the same for `useChat`. A chat in the terminal is `examples/chat/cli.py`, and the same agent behind FastAPI is `examples/chat/api.py`; [the streaming guide](../streaming.md) has the details.
