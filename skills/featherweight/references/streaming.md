<!-- Copied from docs/streaming.md by scripts/build_skill.py. Edit the original, not this file. -->

# Streaming agents

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

An agent that talks to a person while it works: a chat, or any task someone watches. The answer arrives as the model writes it,
as events, and nothing is validated at the end.

Two small examples are in [`examples/chat`](https://github.com/aadhithya/featherweight/blob/main/examples/chat): a chat in the terminal, and the same agent behind FastAPI. The design is in the
[spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/streaming.md).

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

It is declared like any [agent](agents.md): tools, skills, MCP servers and sub-agents work the same. It has no `output_model`
and no `check_output`, and a failed turn is not retried: once a word is out, it cannot be taken back. A sub-agent that it
launches is a tool call that returns when the sub-agent is done; its text is not streamed. A streaming agent cannot itself be
a sub-agent. `allow_user_questions` and `raise_conflicts` do nothing here, because a person is already in the chat.

## Streaming a turn

```python
from ag_ui.core import UserMessage
from featherweight import ChatInput

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

When the loop ends, `run.status` is `SUCCESS`, `FAILED` or `CANCELLED`, `run.error` says why it failed, and `run.usage` has the tokens.
`run.run_id` is the id of the run in the [run log](#what-is-logged).

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
they are shown or not: they are in [`run.new_messages`](#keeping-the-conversation).

## AG-UI

The events are already [AG-UI's](https://docs.ag-ui.com), so the adapter is only the edge with HTTP
(`featherweight.adapters.agui`). There is no server and no router, and it needs no web framework:

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

Sending the events as binary (protobuf) is not supported.

## The conversation is yours

`featherweight` keeps no conversation. A `ChatInput` is the whole conversation so far, and the agent sees only that:

- the **last message is from the user**;
- **`system` and `developer` messages are refused**: the agent has its own prompt, and a client must not be able to replace it;
- `thread_id` is optional. It names the conversation in the run log. It does not load anything.

The history can hold everything a conversation has: text, an assistant message with tool calls, the tool results (with their
error), and thoughts with their signature. A user message may also carry [attachments](#attachments).

## Attachments

A user message can carry images, documents (PDFs and the like), audio and video next to its text. `featherweight` is the **transport**: it
hands them to the model as LangChain content blocks, and the provider decides what it can read.

```python
from ag_ui.core import DocumentInputContent, InputContentDataSource, TextInputContent, UserMessage

pdf = DocumentInputContent(
    source=InputContentDataSource(value=base64_of_the_pdf, mime_type="application/pdf"),
    metadata={"filename": "report.pdf"},  # OpenAI wants a name for an inline file
)
chat_input = ChatInput(messages=[UserMessage(id="1", content=[TextInputContent(text="Summarise this."), pdf])])
```

- **Only inline data.** A part's `source` must be `data`: the bytes, base64-encoded, with a `mime_type`. A `url` source is refused, because
  `featherweight` fetches nothing: your application downloads the file, checks it as it sees fit, and sends the bytes. A file that you
  uploaded to a provider's store (a `file_id`) is not an AG-UI source: build the LangChain message yourself.
- **What you get back.** `ChatInput.from_langchain` and `to_agui` turn the blocks into parts again, so a stored conversation keeps its attachments.
- **Limits.** Every turn sends the whole conversation again, so the limits count the attachments of **all** the messages. They are set on
  the agent, and the defaults are 10 attachments and 15 MB of decoded data:

  ```python
  class Support(StreamingAgent):
      max_attachments = 20
      max_bytes = 30 * 1024 * 1024
  ```

  Over the limit, `runner.stream()` raises a `ValueError` before any run exists. 100 files in one go is a different job: use a [job](submitted-jobs.md).
- **Errors.** What is wrong with the input is raised (a URL, bad base64, no mime type, over a limit). What the provider says no to (a type the
  model cannot read, a file that is too big for it) is a failed turn: a `RUN_ERROR` event and a `FAILED` row, not retried.
- **No checks.** `featherweight` does not know what a model can read. [What the providers accept](attachments-providers.md) is a dated snapshot, and
  the providers' own pages are the source: read them before you design around a type or a size.
- **The Vercel AI SDK** sends an attachment as a `file` part with a `data:` URL: it is read. Any other URL is refused with a `BadRequest`. AG-UI needs nothing extra.
- **Not logged.** The run log, the events and the default observers hold no attachment. An observer that captures content (`capture_content=True`, or
  OpenTelemetry with content capture on) is given the bytes with the rest of the messages: leave it off, or filter, if attachments are private.
- **Some providers drop or refuse a type** without telling you in advance (Anthropic raises for audio and video, and OpenAI's Responses API drops audio
  silently). The [provider page](attachments-providers.md#what-the-langchain-packages-do-with-it) has the table.

Details and decisions: [the attachments spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/attachments.md).

## Keeping the conversation

`featherweight` stores nothing, so the application keeps the messages, with whatever it likes. The format to keep them in is
**LangChain's messages**: its chat message histories read and write them, and they hold what the model needs on the next
request (tool call ids, results, thoughts with their signature). The wire format with a client is AG-UI's. `featherweight` converts
between the two:

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

The converters keep tool call ids, tool results, and thoughts with their signature, so that a stored conversation can go back
to the model. A round trip in either direction gives the same messages, with these exceptions:

- A thought is a message of its own in AG-UI (`reasoning`) and a content block of the assistant message in LangChain. The
  converter folds it into the assistant message that follows it, and unfolds it again.
- A failed tool result has an `error` in AG-UI and one text with `status="error"` in LangChain, so the two texts are one.
- An AG-UI `activity` message is a UI element, not part of the conversation, and is left out.
- A provider's own block format (Claude's `thinking`) is read through LangChain's standard `reasoning` blocks, and kept in that
  form when it comes back from AG-UI.

Any store that takes LangChain messages works: a `BaseChatMessageHistory` from LangChain's packages, or a list in your own
table. There is no LangGraph checkpointer: `featherweight` does not forward one.

## Vercel AI SDK

`featherweight.adapters.vercel` speaks the [UI message stream](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol) that `useChat` and the
other AI SDK clients read. It is not a server: it reads a request and gives you strings, and you send them with any web
framework.

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
- **The version.** Written against the protocol as documented on 2026-11-19. The stream is versioned by its header (`v1`).

## Stopping a response

The person stops the agent while it answers, and then corrects it. Two things stop a run:

```python
run.cancel()  # from anywhere in the same process
runner.cancel(run_id)  # by the id of RUN_STARTED, for a stop button's request; False when no such run is going on here
```

and a **reader that leaves**: when the client drops the connection, your web framework closes the stream, and the run stops.
Close it yourself if you stop reading early: `async with contextlib.aclosing(aiter(run)) as events:` (a plain `break` leaves the
run going until the generator is collected).

**A stop ends the stream cleanly.** The model call is cancelled, everything that was open is closed, and the last event is
`RUN_FINISHED` with the `cancelled` outcome (Vercel: `abort`), so the code after your loop runs. The run's row is `CANCELLED`, its
`output` holds the length of the text so far, and the concurrency slot is free at once. It also works while the run waits for
a slot, and before the loop has started. A second `cancel()`, or one after the run ended, does nothing.

**The partial turn is kept**, so that the person's next message has the context. `run.new_messages` holds what the turn had
said and done, cleaned up so that it is valid to send back:

| When it was stopped | What `new_messages` has |
|---|---|
| Text was streaming | An assistant message with the text so far |
| A tool call's arguments were still streaming | Nothing of that call: it is not complete |
| A tool call was complete and the tool had not returned | The call, and a **tool message that says it did not return** (`status="error"`: "cancelled by the user"), because a provider rejects a call with no result |
| A tool had returned | The call and its real result |
| A thought was streaming | The thought **only if it has its signature** (a provider rejects one without); the text is kept either way |

`new_messages` is filled when the run ends, whatever ended it, so it is there after a reader that left too: save it in a
`finally`, **after the stream is closed** (use `aclosing`, as below, or the run may not be over yet). A turn that **fails** is
closed the same way (its text so far, no call without a result).

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
- A tool that is a synchronous function runs in a thread and cannot be interrupted, and an MCP call may finish on the server:
  its **effect can still happen after the person pressed stop**. An `async` tool is cancelled where it waits.
- A launched sub-agent is cancelled with its parent, and its row is `CANCELLED` too (it is not left `RUNNING`).
- The tokens of a cancelled call are what had been counted when it stopped: the provider may bill for more.
- Across processes: not here. `cancel()` works in the process that runs the stream.

## What is logged

One `attempts` row per turn, in the same [run log](persistence.md#the-tables) as any run, which holds **no content** for any
agent: no messages, no text.

| Column | |
|---|---|
| `input_hash`, `output_hash` | SHA-256 of the `ChatInput`, and of the text (set when the turn succeeds) |
| `thread_id` | The conversation, when the client named one |
| `status` | `INIT` while it waits for a [concurrency slot](agents.md#configuration), `RUNNING` while it streams, then `SUCCESS`, `FAILED` or `CANCELLED` |
| `error_type`, `error_hash` | For a failed turn: the class of the exception and the hash of its message. The client gets the text in the `RUN_ERROR` event |
| tokens, latency, agent, model, the hashes of the agent | As for any run |

The words stay with your application. Save `run_id` next to each turn you store, and you can join your messages to the log.
The slot is held for the whole stream. A streaming run is never reused when a job is [resumed](jobs.md#resuming-a-job).

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

[`examples/chat`](https://github.com/aadhithya/featherweight/blob/main/examples/chat) has one agent (`support.py`, a support chat with one tool, on `claude-haiku-5-5`) and two ways to talk
to it:

- **`cli.py`**, a chat in the terminal in about 30 lines: it prints the answer as it is written, keeps the conversation in a
  list with `run.new_messages`, and Ctrl-C stops the answer so that you can correct the agent. `make example-chat`.
- **`api.py`**, the same agent behind FastAPI: `POST /agui`, `POST /vercel` and `POST /stop/{run_id}`, in about 50 lines.
  The client sends the conversation, so there is no store. `make example-chat-api`.

## Background work

A streaming agent can start another agent and carry on: that is a [background job](background-jobs.md). The turn does not wait for it, the stream
carries two events (`background_started` and `background_finished`), and when the child is done your application starts the next turn with the
history and the child's result (`runner.background_message(job_id)`). A launched sub-agent (`subagents`) is different: it blocks the chat turn
until it returns.

## Not supported

- **Retries.** A failed turn is an error event and a `FAILED` row.
- **Streaming sub-agents.** A launched agent is a tool call with a result.
- **Client-side tools,** shared state and interrupts of AG-UI: ignored.
- **Stored conversations, sessions, a LangGraph checkpointer.** Keep the messages yourself.
- **Structured or validated output** from a streaming agent.
- **Stopping across processes.** `cancel()` works in the process that runs the stream.
- **A web server or router.** The adapters give you strings, you send them.
- **Background jobs** are [their own feature](background-jobs.md), and jobs across processes are [submitted jobs](submitted-jobs.md).
