# A streaming chat agent

A support chat for an online shop, behind a small web app. It shows a [streaming agent](../../docs/streaming.md) from the
browser's side: the answer arrives as it is written, the agent's tool call is an event, and the person can stop it. It uses
`claude-haiku-5-5`, the smallest Claude model, with thinking off, so a conversation costs next to nothing.

```
make example-chat
```

It needs `ANTHROPIC_API_KEY` in `.env`, and serves on `http://localhost:8000`. Without Docker:
`uv run uvicorn --factory --app-dir examples/chat agent:create_app` (`starlette` and `uvicorn` are in the dev group).

## Try it

```
curl -N localhost:8000/chat/t1 -d '{"message": "where is order 1001?"}'
curl localhost:8000/chat/t1                                             # what the server kept
curl -N localhost:8000/chat/t1 -d '{"message": "and 1002?"}'            # the second turn knows the first
curl -X POST localhost:8000/stop/<run id>                               # the run id is in RUN_STARTED
```

| Endpoint | |
|---|---|
| `POST /agui` | The AG-UI protocol. The client sends the whole conversation (a `RunAgentInput`) |
| `POST /vercel` | The Vercel AI SDK: point `useChat` at it. The client sends the whole conversation |
| `POST /chat/{thread_id}` | The server keeps the conversation: send `{"message": "..."}`, get AG-UI events |
| `GET /chat/{thread_id}` | What the server kept, as AG-UI messages |
| `POST /stop/{run_id}` | Stops a response: `runner.cancel(run_id)` |

## What to look at

- **`Support`**: a `StreamingAgent` with one tool, `lookup_order`. To stream its thoughts as well, use
  `Claude(..., thinking="adaptive", show_thoughts=True)`.
- **`talk`**: how an application keeps a conversation, since `micro` keeps none: load the history, stream a turn, and save
  `run.new_messages` in a `finally`, after the stream is closed (`aclosing`), so that a turn the client left halfway is saved
  too, with the partial text and a result for any tool that had not returned.
- **`Store`**: a dict. Put a database behind the same two calls: LangChain's chat message histories do this.
- **`stop`**: a stop button's request. The stream of the stopped run ends cleanly with `RUN_FINISHED` (`cancelled`).

The tests (`tests/test_examples.py`) run this app against a fake model, with no API key.
