<!-- nav:top -->
[Book](../README.md) › [Part 4: Chat](../README.md#part-4-chat) › In the browser  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# In the browser

Send the stream to a browser with AG-UI or the Vercel AI SDK.

<!-- code: code/4-2/app.py -->
```python
"""Chapter 4-2: the same assistant behind a web API, for a browser to talk to."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from featherweight import (
    AgentRunner,
    BadRequest,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    StreamingAgent,
    default_db_path,
)
from featherweight.adapters import agui, vercel


class IncidentAssistant(StreamingAgent):  # a streaming agent answers in a chat: no output model, no retries
    name: str = "incident_assistant"
    description: str = "Answers questions about an incident report"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "The first message is an incident report. Answer questions about it in a sentence or two, only from the report."
    )
    # the model is a setting of the agent
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as app.state.runner:
        yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(BadRequest)
async def bad_request(request: Request, exc: BadRequest):
    return JSONResponse({"error": str(exc)}, status_code=422)  # a body that is not a chat request


@app.post("/vercel")  # the Vercel AI SDK: useChat
async def vercel_chat(request: Request):
    run = request.app.state.runner.stream(IncidentAssistant, vercel.parse_request(await request.body()))
    return StreamingResponse(vercel.encode(run), headers=vercel.HEADERS)


@app.post("/agui")  # an AG-UI client
async def agui_chat(request: Request):
    run = request.app.state.runner.stream(IncidentAssistant, agui.parse_request(await request.body()))
    return StreamingResponse(agui.encode(run), headers=agui.HEADERS, media_type=agui.CONTENT_TYPE)
```

featherweight runs no server. The adapters read a request and turn the events into the stream a client expects, and your web framework sends them: here FastAPI,
which is not in the book's image, so install it where you run the server. In the container (this also publishes the port, which the book's container does not do by default):

```
docker compose run --rm --no-deps -p 8000:8000 book \
  sh -c "pip install -q fastapi uvicorn && python -m uvicorn --app-dir 4-2 app:app --host 0.0.0.0"
```

Without Docker: `pip install fastapi uvicorn`, then `uvicorn --app-dir 4-2 app:app`.

**What to notice**

- **Two endpoints, one agent.** `/vercel` speaks the [Vercel AI SDK](../../streaming.md#vercel-ai-sdk) stream, and `/agui` speaks [AG-UI](../../streaming.md#ag-ui). `parse_request` reads the client's conversation, and `encode` writes the events.
- **The client sends the whole conversation,** report included, on every request. The server keeps nothing.
- **A body that is not a chat request** is a `BadRequest`: answer it with a 422, as the handler does.

A browser using the Vercel AI SDK's `useChat` hook posts to `/vercel`. This is a sketch of the client, not run in the book (the SDK's API changes between versions: check yours):

```tsx
const { messages, sendMessage, stop } = useChat({
  transport: new DefaultChatTransport({ api: "http://localhost:8000/vercel" }),
});
```

The first message the page sends is the report, and the following ones are questions. `stop()` is the next chapter.

> **Why adapters.** The events are one protocol, AG-UI, and each client's format is a translation at the edge. See
> [the spec](../../specs/streaming.md).

Next: stopping a response, and keeping the conversation.

<!-- nav:bottom -->
---

[← A streaming agent](01-a-streaming-agent.md) · [Part 4: Chat](../README.md#part-4-chat) · [Stopping and the conversation →](03-stopping-and-the-conversation.md)
<!-- /nav:bottom -->
