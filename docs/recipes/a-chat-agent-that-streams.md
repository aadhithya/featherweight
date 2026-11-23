# A streaming agent for chat

```python
from contextlib import aclosing

from featherweight import ChatInput, Claude, StreamingAgent
from featherweight.adapters import agui


class Support(StreamingAgent):
    name: str = "support"
    description: str = "Answers customers"
    version: str = "1"
    prompt: str = "You help customers. Be short."
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low", thinking="off")


run = runner.stream(Support, ChatInput.from_langchain([*history.messages, user_message]))
try:
    async with aclosing(agui.encode(run)) as pieces:  # SSE strings: send them with any web framework
        async for piece in pieces:
            yield piece
finally:
    history.add_messages([user_message, *run.new_messages])  # you keep the conversation; featherweight keeps none
```

`runner.cancel(run.run_id)` stops it. `featherweight.adapters.vercel` does the same for `useChat`. A chat in the terminal is
`examples/chat/cli.py`, and the same agent behind FastAPI is `examples/chat/api.py`; [the streaming guide](../streaming.md) has the details. Test it with a fake streaming chat model: the
`Streams` double in the repository's `tests/fakes.py` shows how.
