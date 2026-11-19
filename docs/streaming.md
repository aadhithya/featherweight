# Streaming agents

An agent that talks to a person while it works: a chat, or any task someone watches. The answer arrives as the model writes it,
as events, and nothing is validated at the end.

> **Status: being built** ([epic #29](https://github.com/aadhithya/micro/issues/29)). What works today is on this page. The
> design is in the [spec](specs/streaming.md), which also lists what is still to come: tool calls in the history, stopping a
> response, and the Vercel AI SDK adapter.

- [A streaming agent](#a-streaming-agent)
- [Streaming a turn](#streaming-a-turn)
- [Thoughts](#thoughts)
- [AG-UI](#ag-ui)
- [The conversation is yours](#the-conversation-is-yours)
- [Vercel AI SDK](#vercel-ai-sdk)
- [What is logged](#what-is-logged)
- [How it differs from an agent](#how-it-differs-from-an-agent)
- [What is not there yet](#what-is-not-there-yet)

## A streaming agent

```python
from micro.model_settings import Claude
from micro.streaming import StreamingAgent


class Support(StreamingAgent):
    name: str = "support"
    description: str = "Answers questions about the product"
    version: str = "1"
    prompt: str = "You help customers. Be short."
    model: ModelSettings = Claude(model="claude-sonnet-5-5", effort="low")
    tools: list[BaseTool] = [lookup_order]
```

It is declared like any [agent](agents.md): tools, skills, MCP servers and sub-agents work the same. It has no `output_model`
and no `check_output`, and a failed turn is not retried: once a word is out, it cannot be taken back. A sub-agent that it
launches is a tool call that returns when the sub-agent is done; its text is not streamed. A streaming agent cannot itself be
a sub-agent. `allow_user_questions` and `raise_conflicts` do nothing here, because a person is already in the chat.

## Streaming a turn

```python
from ag_ui.core import UserMessage
from micro.streaming import ChatInput

chat_input = ChatInput(messages=[UserMessage(id="1", content="Where is my order?")], thread_id="t-17")
run = runner.stream(Support, chat_input)
async for event in run:
    print(event.type, getattr(event, "delta", ""))
```

`runner.stream()` returns at once. The run starts when you loop over it, and a run can be looped over once. The events are
[AG-UI's](https://docs.ag-ui.com) (from the `ag-ui-protocol` package), in the order the protocol requires:

| Event | When |
|---|---|
| `RUN_STARTED` | First. It has the `thread_id` (made up when the client named none) and the `run_id` |
| `STEP_STARTED`, `STEP_FINISHED` | One step per model turn, named `model`. A step also holds the tool results that follow the turn |
| `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT` (one per chunk), `TEXT_MESSAGE_END` | The model writes text |
| `REASONING_START`, `REASONING_MESSAGE_START`, `REASONING_MESSAGE_CONTENT`, `REASONING_MESSAGE_END`, `REASONING_END` | The model's thoughts, **only with** [`show_thoughts`](#thoughts) |
| `REASONING_ENCRYPTED_VALUE` | A provider's opaque or signed value for a thought (Claude's signature, OpenAI's encrypted content) |
| `TOOL_CALL_START` (name, id), `TOOL_CALL_ARGS` (the arguments, in pieces), `TOOL_CALL_END` | The model calls a tool |
| `TOOL_CALL_RESULT` | The tool returned (the text of its result, or of its error) |
| `RUN_FINISHED` | Last, on success: `outcome` is `success`, and `usage` has the tokens |
| `RUN_ERROR` | Last, on failure |

The order: a text message, a thought or a tool call is **closed before another kind opens**, and everything that is open
is closed before a step ends and before `RUN_FINISHED` or `RUN_ERROR`. A tool call carries the id of the assistant message of
its turn in `parent_message_id`. A launched sub-agent shows as a tool call (`launch_agent`) with its result: its own words are
not streamed.

When the loop ends, `run.status` is `SUCCESS` or `FAILED`, `run.error` says why, and `run.usage` has the tokens.
`run.run_id` is the id of the run in the [run log](#what-is-logged).

To stop early, leave the loop and close the run (`contextlib.aclosing(run)` does it for you). The run is stopped, its row is
`FAILED` ("Stopped before it finished"), and the slot is given back. A proper stop that keeps the partial turn comes with
[the next parts of the epic](specs/streaming.md#stopping-a-response).

`stream()` raises for a mistake in your code: an agent that is not a `StreamingAgent`, an input that is not a `ChatInput`
(`TypeError`), or a `job_id` that does not exist (raised when the loop starts). A `StreamingAgent` passed to `runner.run()` is a
`TypeError` too. Failures at run time (a provider that is down) are events, not exceptions.

## Thoughts

```python
model: ModelSettings = Claude(model="claude-sonnet-5-5", effort="medium", show_thoughts=True)
```

`show_thoughts` is a setting of [every model class](models.md): it asks the provider to return the model's thoughts, and
the reasoning events carry them. Off by default; with it off there are no reasoning events. It is a summary of the thinking
for most providers, not the raw reasoning. A provider's thoughts that must go back with the next request are kept whether
they are shown or not (that comes with the messages part of the epic).

## AG-UI

The events are already [AG-UI's](https://docs.ag-ui.com), so the adapter is only the edge with HTTP
(`micro.adapters.agui`). There is no server and no router, and it needs no web framework:

| | |
|---|---|
| `parse_request(body)` | The JSON body of an AG-UI request (a dict, text or bytes) as a `ChatInput`. `threadId` is kept. The client's `tools`, `state`, `context` and `forwardedProps` are **ignored** (the agent has its own tools); sending tools logs a warning |
| `encode(run)` | The events of a run as server-sent events, one string each. Leaving the loop early stops the run |
| `CONTENT_TYPE`, `HEADERS` | `text/event-stream`, and headers that stop the stream being cached or buffered |
| `BadRequest` | What `parse_request` raises (a `ValueError`) for a body that is not JSON, is not an AG-UI request, or has a message a `ChatInput` refuses (`system`, `developer`, a last message that is not the user's). The message says what is wrong |

With Starlette (FastAPI is the same):

```python
from micro.adapters import BadRequest
from micro.adapters.agui import CONTENT_TYPE, HEADERS, encode, parse_request
from starlette.responses import JSONResponse, StreamingResponse


async def chat(request):
    try:
        chat_input = parse_request(await request.body())
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    run = runner.stream(Support, chat_input)
    return StreamingResponse(encode(run), media_type=CONTENT_TYPE, headers=HEADERS)
```

Sending the events as binary (protobuf) is not supported.

## The conversation is yours

`micro` keeps no conversation. A `ChatInput` is the whole conversation so far, and the agent sees only that:

- the **last message is from the user**;
- **`system` and `developer` messages are refused**: the agent has its own prompt, and a client must not be able to replace it;
- `thread_id` is optional. It names the conversation in the run log. It does not load anything.

For now the history may have text from the user and the assistant only. Tool calls, tool results and thoughts in the history
come with the messages part of the epic, together with converters to and from LangChain messages, so that you can keep the
conversation in any LangChain chat message store.

## Vercel AI SDK

`micro.adapters.vercel` speaks the [UI message stream](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol) that `useChat` and the
other AI SDK clients read. It is not a server: it reads a request and gives you strings, and you send them with any web
framework.

```python
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from micro.adapters import BadRequest
from micro.adapters.vercel import HEADERS, encode, parse_request

app = FastAPI()


@app.post("/chat")
async def chat(request: Request):
    try:
        chat_input = parse_request(await request.json())
    except BadRequest as exc:
        raise HTTPException(422, str(exc))
    return StreamingResponse(encode(runner.stream(Support, chat_input)), headers=HEADERS)
```

- **The request.** `useChat` posts `{"id": ..., "messages": [UIMessage, ...]}`. `id` becomes the `thread_id`. A UIMessage's
  text parts are joined; an assistant message with tool parts becomes what the model saw (the assistant message with its tool
  calls, then the tool results, then the next step's message), and its reasoning parts become reasoning messages. Files,
  sources and `data-*` parts are ignored. The same rules as any `ChatInput`: no `system` message, and the last message is the
  user's. `parse_request` raises `BadRequest` (a `ValueError`, also raised by the AG-UI adapter) for a body it cannot use, with the reason.
- **The response.** Each AG-UI event becomes the part that the protocol page shows:

  | AG-UI | Part |
  |---|---|
  | `RUN_STARTED` | `start` (its `messageId` is the run id) |
  | `STEP_STARTED`, `STEP_FINISHED` | `start-step`, `finish-step` |
  | `TEXT_MESSAGE_*` | `text-start`, `text-delta`, `text-end` |
  | `REASONING_START`, `REASONING_MESSAGE_CONTENT`, `REASONING_END` | `reasoning-start`, `reasoning-delta`, `reasoning-end` |
  | `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END` | `tool-input-start`, `tool-input-delta`, `tool-input-available` (the arguments, parsed) |
  | `TOOL_CALL_RESULT` | `tool-output-available` (the result, parsed when it is JSON) |
  | `RUN_FINISHED` | `finish`, or `abort` when the run was stopped |
  | `RUN_ERROR` | `error` |

  The stream ends with `data: [DONE]`. `HEADERS` has `x-vercel-ai-ui-message-stream: v1`. Events with no part (state,
  activity, and the opaque value of a thought) are dropped: the Vercel client does not need them, and you keep the messages
  yourself.
- **A client that goes away.** Stop iterating `encode(...)` (a web framework does when the connection drops) and the run stops.
- **The version.** Written against the protocol as documented on 2026-11-19. The stream is versioned by its header (`v1`).

## What is logged

One `agent_runs` row per turn, in the same [run log](agents.md#jobs-and-tracing) as any run, **with no message content**:

| Column | |
|---|---|
| `input` | Only `{"messages": 7}`, the number of messages |
| `output` | Only `{"chars": 412}`, the length of the text |
| `input_hash`, `output_hash` | SHA-256 of the `ChatInput`, and of the text |
| `thread_id` | The conversation, when the client named one |
| `status` | `INIT` while it waits for a [concurrency slot](agents.md#configuration), `RUNNING` while it streams, then `SUCCESS` or `FAILED` |
| tokens, latency, agent, model, `agent_hash` | As for any run |

The words stay with your application. Save `run_id` next to each turn you store, and you can join your messages to the log.
The slot is held for the whole stream. A streaming run is never reused when a job is [resumed](agents.md#resuming-a-job).

## How it differs from an agent

| | `AgentBase` | `StreamingAgent` |
|---|---|---|
| Run with | `runner.run()` | `runner.stream()` |
| Input | its own `input_model` | `ChatInput` |
| Output | `output_model`, validated, `check_output` | text, in events, not validated |
| Retries | up to `max_attempts` | none |
| Rows | one per attempt | one per turn |
| System prompt | the prompt, plus the instructions for questions and conflicts | the prompt |

## What is not there yet

Tracked in [epic #29](https://github.com/aadhithya/micro/issues/29): messages in and out
(#32), stopping a response (#33), the Vercel AI SDK adapter (#35), an example and the full guide (#36).
Background agents, jobs across processes and stored conversations are not part of it.
