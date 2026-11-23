# Streaming agents

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

An agent that talks to a person while it works, such as a chat. The answer arrives as events while the model writes it, and nothing is validated at the end.

Examples: [`examples/chat`](../examples/chat). Design: the [spec](specs/streaming.md).

- [A streaming agent](#a-streaming-agent)
- [Streaming a turn](#streaming-a-turn)
- [Thoughts](#thoughts)
- [AG-UI](#ag-ui)
- [The conversation is yours](#the-conversation-is-yours)
- [Keeping the conversation](#keeping-the-conversation)
- [Vercel AI SDK](#vercel-ai-sdk)
- [Stopping a response](#stopping-a-response)
- [The example](#the-example)
- [What is logged](#what-is-logged)
- [How it differs from an agent](#how-it-differs-from-an-agent)
- [Background work](#background-work)
- [Not supported](#not-supported)

## A streaming agent

```python
from featherweight import Claude, StreamingAgent


class Support(StreamingAgent):
    name: str = "support"
    description: str = "Answers questions about the product"
    version: str = "1"
    prompt: str = "You help customers. Be short."
    model: ModelSettings = Claude(model="claude-sonnet-5-5", effort="low")
    tools: list[BaseTool] = [lookup_order]
```

It is declared like any [agent](agents.md): tools, skills, MCP servers and sub-agents work the same. It has no `output_model` or `check_output`, and a failed turn is not retried, because a word that is out cannot be taken back. A sub-agent it launches is a tool call that returns when the sub-agent is done, and its text is not streamed. A streaming agent cannot be a sub-agent. `allow_user_questions` and `raise_conflicts` do nothing, because a person is already in the chat.

## Streaming a turn

```python
from ag_ui.core import UserMessage
from featherweight import ChatInput

chat_input = ChatInput(messages=[UserMessage(id="1", content="Where is my order?")], thread_id="t-17")
run = runner.stream(Support, chat_input)
async for event in run:
    print(event.type, getattr(event, "delta", ""))
```

`runner.stream()` returns at once. The run starts when you loop over it, and can be looped over once. The events are [AG-UI's](https://docs.ag-ui.com) (from the `ag-ui-protocol` package), in the order the protocol requires:

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

A text message, a thought or a tool call is **closed before another kind opens**, and everything open is closed before a step ends and before `RUN_FINISHED` or `RUN_ERROR`. A tool call carries the id of its turn's assistant message in `parent_message_id`. A launched sub-agent shows as a tool call (`launch_agent`) with its result.

When the loop ends, `run.status` is `SUCCESS`, `FAILED` or `CANCELLED`, `run.error` says why it failed, and `run.usage` has the tokens. `run.run_id` is the id of the run in the [run log](#what-is-logged).

`stream()` raises for a mistake in your code: an agent that is not a `StreamingAgent` or an input that is not a `ChatInput` (`TypeError`), or a `job_id` that does not exist (raised when the loop starts). A `StreamingAgent` passed to `runner.run()` is a `TypeError` too. Failures at run time, such as a provider that is down, are events, not exceptions.

## Thoughts

```python
model: ModelSettings = Claude(model="claude-sonnet-5-5", effort="medium", show_thoughts=True)
```

`show_thoughts` is a setting of [every model class](models.md). It asks the provider for the model's thoughts, which the reasoning events carry; off by default, and then there are no reasoning events. For most providers it is a summary, not the raw reasoning. Thoughts that must go back with the next request are kept whether shown or not, in [`run.new_messages`](#keeping-the-conversation).

## AG-UI

The events are already AG-UI's, so the adapter (`featherweight.adapters.agui`) is only the edge with HTTP. It has no server or router and needs no web framework:

| | |
|---|---|
| `parse_request(body)` | The JSON body of an AG-UI request (a dict, text or bytes) as a `ChatInput`. `threadId` is kept. The client's `tools`, `state`, `context` and `forwardedProps` are **ignored** (the agent has its own tools); sending tools logs a warning |
| `encode(run)` | The events of a run as server-sent events, one string each. Leaving the loop early stops the run |
| `CONTENT_TYPE`, `HEADERS` | `text/event-stream`, and headers that stop the stream being cached or buffered |
| `BadRequest` | What `parse_request` raises (a `ValueError`) for a body that is not JSON, is not an AG-UI request, or has a message a `ChatInput` refuses (`system`, `developer`, a last message that is not the user's). The message says what is wrong |

With Starlette (FastAPI is the same):

```python
from featherweight import BadRequest
from featherweight.adapters import agui
from starlette.responses import JSONResponse, StreamingResponse


async def chat(request):
    try:
        chat_input = agui.parse_request(await request.body())
    except BadRequest as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    run = runner.stream(Support, chat_input)
    return StreamingResponse(agui.encode(run), media_type=agui.CONTENT_TYPE, headers=agui.HEADERS)
```

Binary (protobuf) events are not supported.

## The conversation is yours

`featherweight` keeps no conversation. A `ChatInput` is the whole conversation so far, and it is all the agent sees:

- the **last message is from the user**;
- **`system` and `developer` messages are refused**: the agent has its own prompt, and a client must not be able to replace it;
- `thread_id` is optional. It names the conversation in the run log. It does not load anything.

The history can hold text, assistant messages with tool calls, tool results (with their error), and thoughts with their signature. A user message may also carry [attachments](#attachments).

## Attachments

A user message can carry images, documents, audio and video next to its text. `featherweight` only **transports** them, as LangChain content blocks. The provider decides what it can read.

```python
from ag_ui.core import DocumentInputContent, InputContentDataSource, TextInputContent, UserMessage

pdf = DocumentInputContent(
    source=InputContentDataSource(value=base64_of_the_pdf, mime_type="application/pdf"),
    metadata={"filename": "report.pdf"},  # OpenAI wants a name for an inline file
)
chat_input = ChatInput(messages=[UserMessage(id="1", content=[TextInputContent(text="Summarise this."), pdf])])
```

- **Only inline data.** A part's `source` must be `data`: base64 bytes with a `mime_type`. A `url` source is refused, because `featherweight` fetches nothing: your application downloads and checks the file, and sends the bytes. A file you uploaded to a provider (a `file_id`) is not an AG-UI source: build the LangChain message yourself.
- **Round trip.** `ChatInput.from_langchain` and `to_agui` turn the blocks into parts again, so a stored conversation keeps its attachments.
- **Limits.** Every turn resends the whole conversation, so the limits count the attachments of **all** messages. They are set on the agent; the defaults are 10 attachments and 15 MB of decoded data:

  ```python
  class Support(StreamingAgent):
      max_attachments = 20
      max_bytes = 30 * 1024 * 1024
  ```

  Over the limit, `runner.stream()` raises a `ValueError` before any run exists. For 100 files at once, use a [job](submitted-jobs.md).
- **Errors.** A wrong input is raised (a URL, bad base64, no mime type, over a limit). What the provider refuses (a type the model cannot read, a file too big for it) is a failed turn: a `RUN_ERROR` event and a `FAILED` row, not retried.
- **No checks.** `featherweight` does not know what a model can read. [What the providers accept](attachments-providers.md) is a dated snapshot; read the providers' own pages before you design around a type or a size.
- **The Vercel AI SDK** sends an attachment as a `file` part with a `data:` URL, which is read. Any other URL is refused with a `BadRequest`.
- **Not logged.** The run log, the events and the default observers hold no attachment. An observer that captures content (`capture_content=True`, or OpenTelemetry with content capture on) gets the bytes with the other messages: leave it off, or filter, if attachments are private.
- **Some providers drop or refuse a type** without warning (Anthropic raises for audio and video; OpenAI's Responses API silently drops audio). The [provider page](attachments-providers.md#what-the-langchain-packages-do-with-it) has the table.

Details and decisions: [the attachments spec](specs/attachments.md).

## Keeping the conversation

`featherweight` stores nothing: the application keeps the messages, wherever it likes, as **LangChain's messages**. LangChain's chat message histories read and write them, and they hold what the model needs on the next request (tool call ids, results, thoughts with their signature). The wire format with a client is AG-UI's, and `featherweight` converts between the two:

```python
run = runner.stream(Support, ChatInput.from_langchain([*history.messages, user_message], thread_id=thread_id))
async for event in run:
    yield encoder.encode(event)  # to the browser
history.add_messages([user_message, *run.new_messages])  # to your store
```

| | |
|---|---|
| `run.new_messages` | The LangChain messages of the turn: the assistant messages (with their tool calls and thoughts) and the tool results, in order. They fill up while the turn runs and are complete when the loop ends. After a failure it holds what had finished |
| `ChatInput.from_langchain(messages, thread_id=None)` | The conversation from LangChain messages, such as `history.messages`. The last one is the user's |
| `ChatInput.from_agui(request_or_messages, thread_id=None)` | The conversation from what an AG-UI client sent: a `RunAgentInput` (its `thread_id` is kept) or its messages |
| `featherweight.messages.to_langchain(messages)`, `to_agui(messages)` | The converters, in both directions |

The converters keep tool call ids, tool results and signed thoughts, so a stored conversation can go back to the model. A round trip gives the same messages, except:

- A thought is a message of its own in AG-UI (`reasoning`) and a content block of the assistant message in LangChain. The
  converter folds it into the assistant message that follows it, and unfolds it again.
- A failed tool result has an `error` in AG-UI and one text with `status="error"` in LangChain, so the two texts are one.
- An AG-UI `activity` message is a UI element, not part of the conversation, and is left out.
- A provider's own block format (Claude's `thinking`) is read through LangChain's standard `reasoning` blocks, and kept in that
  form when it comes back from AG-UI.

Any store that takes LangChain messages works: a `BaseChatMessageHistory`, or a list in your own table. There is no LangGraph checkpointer.

## Vercel AI SDK

`featherweight.adapters.vercel` speaks the [UI message stream](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol) that `useChat` and the other AI SDK clients read. It is not a server: it reads a request and gives you strings to send with any web framework.

```python
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from featherweight import BadRequest
from featherweight.adapters import vercel

app = FastAPI()


@app.post("/chat")
async def chat(request: Request):
    try:
        chat_input = vercel.parse_request(await request.json())
    except BadRequest as exc:
        raise HTTPException(422, str(exc))
    return StreamingResponse(vercel.encode(runner.stream(Support, chat_input)), headers=vercel.HEADERS)
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
- **The version.** Written against the protocol as documented on 2026-11-19, and versioned by its header (`v1`).

## Stopping a response

The person stops the agent while it answers, then corrects it. Two things stop a run:

```python
run.cancel()  # from anywhere in the same process
runner.cancel(run_id)  # by the id of RUN_STARTED, for a stop button's request; False when no such run is going on here
```

and a **reader that leaves**: when the client drops the connection, your web framework closes the stream and the run stops. If you stop reading early, close it yourself with `async with contextlib.aclosing(aiter(run)) as events:`. A plain `break` leaves the run going until the generator is collected.

**A stop ends the stream cleanly.** The model call is cancelled, everything open is closed, and the last event is `RUN_FINISHED` with the `cancelled` outcome (Vercel: `abort`), so the code after your loop runs. The row is `CANCELLED`, its `output` holds the length of the text so far, and the concurrency slot is free at once. A stop also works while the run waits for a slot, and before the loop has started. A second `cancel()`, or one after the run ended, does nothing.

**The partial turn is kept**, so the next message has its context. `run.new_messages` holds what the turn said and did, cleaned up so it is valid to send back:

| When it was stopped | What `new_messages` has |
|---|---|
| Text was streaming | An assistant message with the text so far |
| A tool call's arguments were still streaming | Nothing of that call: it is not complete |
| A tool call was complete and the tool had not returned | The call, and a **tool message that says it did not return** (`status="error"`: "cancelled by the user"), because a provider rejects a call with no result |
| A tool had returned | The call and its real result |
| A thought was streaming | The thought **only if it has its signature** (a provider rejects one without); the text is kept either way |

`new_messages` is filled when the run ends, whatever ended it, including a reader that left. Save it in a `finally`, **after the stream is closed** (use `aclosing`, as below, or the run may not be over yet). A turn that **fails** is closed the same way: its text so far, no call without a result.

```python
from contextlib import aclosing

run = runner.stream(Support, ChatInput.from_langchain([*history.messages, user_message]))
try:
    async with aclosing(agui.encode(run)) as pieces:
        async for piece in pieces:
            yield piece
finally:
    history.add_messages([user_message, *run.new_messages])
```

**What a stop cannot undo**
- A synchronous tool runs in a thread and cannot be interrupted, and an MCP call may finish on the server, so its **effect can still happen after the person pressed stop**. An `async` tool is cancelled where it waits.
- A launched sub-agent is cancelled with its parent, and its row is `CANCELLED` too.
- The tokens of a cancelled call are those counted when it stopped; the provider may bill for more.
- `cancel()` works only in the process that runs the stream.

## What is logged

One `attempts` row per turn, in the same [run log](persistence.md#the-tables) as any run. It holds **no content**: no messages, no text.

| Column | |
|---|---|
| `input_hash`, `output_hash` | SHA-256 of the `ChatInput`, and of the text (set when the turn succeeds) |
| `thread_id` | The conversation, when the client named one |
| `status` | `INIT` while it waits for a [concurrency slot](agents.md#configuration), `RUNNING` while it streams, then `SUCCESS`, `FAILED` or `CANCELLED` |
| `error_type`, `error_hash` | For a failed turn: the class of the exception and the hash of its message. The client gets the text in the `RUN_ERROR` event |
| tokens, latency, agent, model, the hashes of the agent | As for any run |

The words stay with your application: save `run_id` next to each turn you store, to join your messages to the log. The slot is held for the whole stream. A streaming run is never reused when a job is [resumed](jobs.md#resuming-a-job).

## How it differs from an agent

| | `AgentBase` | `StreamingAgent` |
|---|---|---|
| Run with | `runner.run()` | `runner.stream()` |
| Input | its own `input_model` | `ChatInput` |
| Output | `output_model`, validated, `check_output` | text, in events, not validated |
| Retries | up to `max_attempts` | none |
| Rows | one per attempt | one per turn |
| System prompt | the prompt, plus the instructions for questions and conflicts | the prompt |

## The example

[`examples/chat`](../examples/chat) has one agent (`support.py`: a support chat with one tool, on `claude-haiku-5-5`) and two ways to talk to it:

- **`cli.py`**, a terminal chat in about 30 lines. It prints the answer as it is written, keeps the conversation in a list with `run.new_messages`, and Ctrl-C stops the answer so you can correct the agent. `make example-chat`.
- **`api.py`**, the same agent behind FastAPI in about 50 lines: `POST /agui`, `POST /vercel` and `POST /stop/{run_id}`. The client sends the conversation, so there is no store. `make example-chat-api`.

## Background work

A streaming agent can start another agent and carry on: a [background job](background-jobs.md). The turn does not wait for it. The stream carries two events (`background_started` and `background_finished`), and when the child is done your application starts the next turn with the history and the child's result (`runner.background_message(job_id)`). A launched sub-agent (`subagents`) blocks the turn until it returns.

## Not supported

- **Retries.** A failed turn is an error event and a `FAILED` row.
- **Streaming sub-agents:** a launched agent is a tool call with a result.
- **Client-side tools,** shared state and interrupts of AG-UI: ignored.
- **Stored conversations, sessions, a LangGraph checkpointer.** Keep the messages yourself.
- **Structured or validated output** from a streaming agent.
- **Stopping across processes:** `cancel()` works in the process that runs the stream.
- **A web server or router.** The adapters give you strings, you send them.
- **Background jobs** are [their own feature](background-jobs.md), and jobs across processes are [submitted jobs](submitted-jobs.md).
