# A streaming chat agent

One agent, a support chat for an online shop, and two ways to talk to it. It uses `claude-haiku-5-5`, the smallest Claude
model, with thinking off, so a conversation costs next to nothing. It needs `ANTHROPIC_API_KEY` in `.env`.

[`support.py`](support.py) is the agent: a class, a prompt, one tool. This is all it takes to define a
[streaming agent](../../docs/streaming.md).

## In the terminal

```
make example-chat
```

[`cli.py`](cli.py) is about 30 lines. It prints the answer as it is written and keeps the conversation in a list. **Press Ctrl-C
while it answers** to stop it, then type a correction: the agent has what it had said so far. An empty line leaves.
Without Docker: `uv run python examples/chat/cli.py`.

## Behind an API

```
make example-chat-api
```

[`api.py`](api.py) puts the same agent behind FastAPI on `http://localhost:8000`. Point an AG-UI client at `/agui`, or
`useChat` of the Vercel AI SDK at `/vercel`; `POST /stop/<run id>` stops a response (the run id is in the first event). The client
sends the conversation, so the server keeps none. Without Docker:
`uv run uvicorn --app-dir examples/chat api:app`.

To stream the model's thoughts too, use `Claude(..., thinking="adaptive", show_thoughts=True)` in `support.py`.

## With a file

```
make example-chat-attach FILE=photo.png Q="What is in this picture?"
```

[`attach.py`](attach.py) sends a question with an image, a PDF, a recording or a video, as inline data: `featherweight` only passes the
file on, and fetches nothing (a URL is refused: you download it and send the bytes). The agent sets its own limit of one file; the defaults are
10 files and 15 MB. Whether the model can read the type is the provider's to say, and a no comes back as a failed turn, which the example
prints. See [attachments](../../docs/streaming.md#attachments) and [what the providers accept](../../docs/attachments-providers.md).

The tests (`tests/test_examples.py`) run all three against a fake model, with no API key.
