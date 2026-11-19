# Spec: streaming agents

Status: being built. #30 (the class, `ChatInput`, `runner.stream()`, the run log), #31 (the events: text, tool calls, thoughts, steps; `show_thoughts`), #32 (messages in and out: the converters, `from_langchain`, `from_agui`, `new_messages`), #34 (the AG-UI adapter) and #35 (the Vercel adapter) are done; the other issues are not. It is tracked in [epic #29](https://github.com/aadhithya/micro/issues/29), and each part has its own
issue (listed [at the end](#the-work)). When a part is built, this page says so and the guide `docs/streaming.md` shows how to use it.

It changes [agents](agents.md) (a new kind of agent), [the runner](runner.md) (`stream()`, and `run()` refuses a streaming
agent), [models](models.md) (`show_thoughts`) and [jobs and tracing](jobs-and-tracing.md) (a `thread_id` column and a
`CANCELLED` status).

- [Goal](#goal)
- [The agent and its input](#the-agent-and-its-input)
- [Streaming a run](#streaming-a-run)
- [The events](#the-events)
- [The model's thoughts](#the-models-thoughts)
- [Messages and keeping the conversation](#messages-and-keeping-the-conversation)
- [Stopping a response](#stopping-a-response)
- [The run log](#the-run-log)
- [The adapters](#the-adapters)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Findings](#findings)
- [Tests](#tests)
- [The work](#the-work)

## Goal

Some agents talk to a person: a chat, or a task that a person watches. They must show the answer **while it is written**,
show what the agent is doing (the tools it calls, and its thoughts), let the person **stop** it and correct it, and not wait
for a finished, validated output. `micro` adds this without a second runtime: a streaming agent is an agent, run by the same
runner, with the same tools, skills, MCP servers, limits and run log.

We build abstractions, not a solution. The events are an open protocol's, the HTTP and the storage are the application's.

## The agent and its input

```python
class Support(StreamingAgent):
    name: str = "support"
    description: str = "Answers questions about the product"
    version: str = "1"
    prompt: str = "You help customers. Be short."
    model: ModelSettings = Claude(model="claude-sonnet-5-5", effort="low", show_thoughts=True)
    tools: list[BaseTool] = [lookup_order]
```

`StreamingAgent` extends `AgentBase`. What changes:

| | `AgentBase` | `StreamingAgent` |
|---|---|---|
| `input_model` | the agent's own | `ChatInput` (fixed) |
| `output_model`, `check_output` | yes | **none**: nothing is validated, the output is text |
| Retries | `max_attempts` | **none**: once a token is out, it cannot be taken back |
| Run with | `runner.run()` | `runner.stream()` |

Passing a `StreamingAgent` to `run()` raises `TypeError`, and so does passing any other agent to `stream()`, naming the other
method. Tools, `subagents`, `skills`, `mcp_servers` and discovery are declared and checked as they are for any agent.

**`ChatInput`** is the conversation so far, sent by the client:

```python
ChatInput(messages=[...], thread_id="t-17")  # messages: AG-UI Message types
```

It is checked when built:

- The **last message is from the user.**
- **`system` and `developer` messages are rejected.** The agent has its own prompt, and a client must not be able to replace
  it. (The AG-UI request has these roles; a client that sends one gets an error, not silence.)
- `thread_id` is optional. It identifies the conversation for the run log; it does not load anything.

## Streaming a run

```python
run = runner.stream(Support, chat_input, job_id=None)  # nothing happens yet
async for event in run:  # AG-UI events, as the model writes
    send(event)
run.new_messages  # LangChain messages of this turn (after the loop)
```

`StreamingRun`:

| Member | |
|---|---|
| `async for event in run` | The events, once. A second loop is an error |
| `run_id` | The id in `RUN_STARTED`, and what `runner.cancel()` takes |
| `new_messages` | The LangChain messages this turn produced. Complete when the stream ends, [even on cancel](#stopping-a-response) |
| `usage` | Input and output tokens, when known |
| `status`, `error` | `RUNNING`, then `SUCCESS`, `FAILED` or `CANCELLED`, and the error text |
| `cancel()` | [Stops the run](#stopping-a-response) |

Under it, the runner builds the agent as it does for `run()` (tools, MCP lease, skills, discovery, the system prompt) and
streams the LangChain agent with `astream`. The run waits for a concurrency slot (the row stays `INIT`), **holds the slot until
the stream ends**, and gives it back on success, failure and cancel.

A launched sub-agent is a tool call that blocks until it returns: it shows as a tool call with a result, and its own tokens are
not streamed. (The depth and launch rules are the same as in [launching agents](runner.md#launching-agents).)

## The events

The events are **AG-UI's**, from the official `ag-ui-protocol` package (1.0, Pydantic models). `micro` defines none. The
runner produces them in the order the protocol requires, and the adapters send them.

| What happened | Events |
|---|---|
| The run starts | `RUN_STARTED` (thread id, run id) |
| A model turn starts, ends | `STEP_STARTED`, `STEP_FINISHED` (the step name is `model`) |
| The model writes text | `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT` (one per chunk), `TEXT_MESSAGE_END` |
| The model thinks (`show_thoughts`) | `REASONING_START`, `REASONING_MESSAGE_START`, `REASONING_MESSAGE_CONTENT`, `REASONING_MESSAGE_END`, `REASONING_END` |
| A provider's opaque reasoning value | `REASONING_ENCRYPTED_VALUE` |
| The model calls a tool | `TOOL_CALL_START`, `TOOL_CALL_ARGS` (the arguments stream), `TOOL_CALL_END` |
| The tool returns | `TOOL_CALL_RESULT` (with the error text, when the tool failed) |
| The run ends well | `RUN_FINISHED` (success outcome), with the token usage |
| The run is stopped | `RUN_FINISHED` with the `cancelled` outcome |
| The run fails | `RUN_ERROR` (the open message is closed first) |

Rules the runner keeps: a text message is closed before a tool call opens; ids (message, tool call, reasoning) are made by
`micro` when the provider gives none; every opened thing is closed, also on error and on cancel; one `RUN_STARTED` and one
ending event per run. A test checks every produced sequence against these rules.

How LangChain's stream maps to this (to be confirmed while building, see [Findings](#findings)): `astream` with the
`messages` mode gives the chunks of the model's output (text, reasoning and tool call chunks, as content blocks), and the
`updates` mode gives what a tool node returned.

Events with no source here (state, activity, custom, sub-agent events) are not produced. The custom event is reserved for the
[background agents epic](#not-supported).

## The model's thoughts

`ModelSettings` gets one field, `show_thoughts: bool = False`. Each provider class maps it to its own option: Gemini and
Gemma ask the model to include its thought summaries, OpenAI asks for a reasoning summary, and Claude asks for its thinking
to be returned. (The settings today only set **how hard** a model thinks.)

- Off (the default): no reasoning events.
- On: the reasoning is streamed as reasoning events and kept in the messages.
- What a thought is differs by provider: Claude returns thinking blocks, OpenAI a **summary** (its raw reasoning is hidden),
  Gemini a thought summary. The guide says so.
- **Kept either way.** A provider's reasoning, and its opaque or signed value (`REASONING_ENCRYPTED_VALUE`, a signature), is
  always kept in `new_messages`, even when it is not shown, because the provider needs it back on the next request when the
  model thinks between tool calls.

It is a field of `ModelSettings`, so it is available to every agent, and it is recorded with the other settings in the run log.

## Messages and keeping the conversation

**`micro` keeps no conversation.** The client sends the whole history with each request. That is how both protocols work, and
it means any process can serve any turn. The application keeps messages if it wants to, with the tools LangChain already has.

- Wire format: AG-UI messages. Storage format: **LangChain messages**, because LangChain's chat message histories read and
  write them, and because they hold what the model needs (tool call ids, usage, reasoning blocks).
- Converters, in both directions, between AG-UI and LangChain messages: user, assistant (text and tool calls), tool results (with
  their error), and reasoning (with its encrypted value).
- `ChatInput.from_langchain(messages, thread_id=None)`: history loaded from a store.
- `ChatInput.from_agui(messages_or_request)`: what a client sent.
- A LangChain to AG-UI converter: to rebuild what a client shows from stored history.
- `StreamingRun.new_messages`: the LangChain messages of the turn.

```python
history = SQLChatMessageHistory(session_id=thread_id, connection=...)  # or any BaseChatMessageHistory
run = runner.stream(Support, ChatInput.from_langchain([*history.messages, user_message], thread_id))
async for event in run:
    yield encoder.encode(event)
history.add_messages([user_message, *run.new_messages])
```

The converters keep tool call ids, tool results, and reasoning (with signatures) **exactly**; a round trip is tested. Anything the
other format cannot hold is listed in the guide.

**No LangGraph checkpointer.** `create_agent` supports one, and LangChain recommends it for agents, but it stores the whole graph
state per thread, and it is not how `micro` passes state around. It is not forwarded. A later issue can add it if someone asks.

## Stopping a response

The person stops the agent while it answers, then corrects it: "no, not that one".

**Two ways to stop**

1. **The client drops the stream** (Vercel's `stop()` aborts the fetch, and an AG-UI client closes the connection). The consumer
   closes the iterator, and the run is cancelled.
2. **An explicit cancel**: `run.cancel()`, or `runner.cancel(run_id)` from another request in the same process (the runner keeps
   its live streaming runs by id). Across processes it belongs to the [jobs epic](#not-supported).

An explicit cancel ends the stream **cleanly**: the model call is cancelled, `RUN_FINISHED` with the `cancelled` outcome is
sent, and the loop ends, so the application's code after it runs. A disconnect cancels the same way, with no one listening, so
it saves in a `finally` (the guide shows it).

**The partial turn is kept.** The next request has to contain what the agent already said and did, and a tool that already ran
had its effect. So after a cancel, `new_messages` holds the partial turn, cleaned up so that it is valid to send back:

| State when stopped | What `new_messages` has |
|---|---|
| Text was streaming | The assistant message with the text so far |
| Thoughts were streaming | The reasoning so far |
| A tool call's arguments were still streaming | That tool call is **dropped** (it is not complete) |
| A tool call was complete, the tool had not returned | The tool call, **and a tool message with an error: "cancelled by the user"**, because a provider rejects a tool call that has no result |
| A tool had returned | The call and its real result |

`new_messages` is filled in a `finally` inside the runner, so it is there after a disconnect too.

**What a stop does not do**

- A synchronous tool running in a thread cannot be interrupted, and an MCP call may finish on the server. **A side effect can
  still complete after the person pressed stop.** The guide says it.
- A cancelled call's token usage may be unknown. The row records what is known.
- A launched sub-agent is cancelled with its parent.
- The concurrency slot is released (tested at each point above, and while waiting for the slot).

## The run log

One `agent_runs` row per turn, as for any run, with these differences:

| | |
|---|---|
| `status` | `INIT` while it waits for a slot, `RUNNING` while it streams, then `SUCCESS`, `FAILED` or **`CANCELLED`** (new; the column is text, so no migration) |
| `input` | **No messages are stored.** A small placeholder with the message count, such as `{"messages": 7}` (the column is `NOT NULL` in sqlite, so it cannot be left empty), and `input_hash`, the hash of the whole `ChatInput` |
| `output` | **No text is stored.** A placeholder with the length, such as `{"chars": 412}`, and an `output_hash` (new nullable column). For a cancel, the length so far |
| `thread_id` | **New nullable column.** The conversation, so its turns can be found |
| `agent_hash` | Recorded as for any run |
| Reuse | **None.** Resuming a job does not reuse a streaming run |

The run's `job_id` works as for `run()`. Marking a *job* `CANCELLED` is part of the jobs epic.

## The adapters

An adapter has two parts: **read the request** into a `ChatInput`, and **encode the events** for the wire. Neither is a server:
no router, no web framework dependency. A one-line Starlette example goes in the guide.

**AG-UI** (`micro.adapters.agui`, built): the events are already AG-UI's, so this is the thin edge.
- `parse_request(body)`: a `RunAgentInput` (a dict, text or bytes) becomes a `ChatInput`; a body it cannot read raises `BadRequest`, a `ValueError` that a framework turns into a 422. The client's `tools`, `state` and `context` are **ignored**
  (the agent has its own tools), and sending tools logs a warning.
- `encode(run)`: SSE strings from the official `EventEncoder`, plus `CONTENT_TYPE` and `HEADERS` for the response. The binary encoding is not supported.

**Vercel AI SDK** (`micro.adapters.vercel`): a translation from AG-UI events to the documented UI message stream, written by us
because no small official package exists (the one from vercel-labs is a whole agent framework, at 0.x).

| AG-UI | Vercel part |
|---|---|
| `RUN_STARTED` | `start` |
| `STEP_STARTED`, `STEP_FINISHED` | `start-step`, `finish-step` |
| `TEXT_MESSAGE_*` | `text-start`, `text-delta`, `text-end` |
| `REASONING_*` | `reasoning-start`, `reasoning-delta`, `reasoning-end` |
| `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END` | `tool-input-start`, `tool-input-delta`, `tool-input-available` |
| `TOOL_CALL_RESULT` | `tool-output-available` |
| `RUN_FINISHED` (success) | `finish`, then `[DONE]` |
| `RUN_FINISHED` (cancelled) | `abort`, then `[DONE]` |
| `RUN_ERROR` | `error`, then `[DONE]` |

The header is `x-vercel-ai-ui-message-stream: v1`. The request is Vercel's list of `UIMessage` (messages made of parts:
text, reasoning, tool parts with a state), converted to a `ChatInput` with the same rules as AG-UI. State and activity events
have no Vercel part and are dropped. The guide names the protocol version we wrote against and the date we checked.

A third format is a third adapter: the events are the contract.

## Decisions

- **The events are AG-UI's.** We write only what does not exist: the mapping from LangChain's stream, the converters, and the
  Vercel translation. It ties us to a spec we do not control; it is open, at 1.0, and the format that Pydantic AI and others
  already speak.
- **No retries.** A failure is an error event and a `FAILED` row.
- **Stateless on our side.** No sessions, no conversation store, no checkpointer. The run log holds **no message content** either, only counts, hashes,
  tokens, status and timing: `micro` is not a persistence layer. The app keeps the words (with LangChain's stores) and saves the
  `run_id` from `RUN_STARTED` next to each turn, so it can join its messages to the log. Where the log should keep content for
  all agents is a wider question, parked in the ideas notes (the run log and persistence boundary).
- **Client history is untrusted:** no `system` or `developer` roles; the application still validates what it stores and sends.
- **Cancel keeps the partial turn,** with a synthetic result for a tool call that did not return.
- **`show_thoughts` is a `ModelSettings` field, default off.** (Taken in this spec; to confirm in review: a streaming agent
  could default it on instead.)
- **`thread_id` is a column,** so a conversation is traceable without a conversation store. (Taken in this spec; to confirm.)
- **A custom `StreamingAgent` output is text only.** Structured output from a streaming agent is a different feature, and is not
  here.

## Not supported

- **Background agents** (a sub-agent that runs while the chat goes on, and wakes the next turn when done). Parked as its own
  epic, after jobs. The application can do it today with a job and by starting the next turn itself; the guide shows how.
- **Cancelling across processes**, and queued or multi-process jobs: the jobs epic.
- **Streaming sub-agents:** a launched sub-agent is a tool call.
- **Client-side tools** (AG-UI's `tools`), shared state, activity, interrupts and resume: ignored.
- **Stored conversations, sessions, a LangGraph checkpointer.**
- **The AG-UI binary encoding**, a router or server.
- **Structured or validated output** from a streaming agent.

## Findings

Checked while writing this (2026-11-19):

- `ag-ui-protocol` 1.0.0 on PyPI: depends on Pydantic only, Python >= 3.9. Its models include the text, tool call, reasoning
  (`REASONING_*` and `REASONING_ENCRYPTED_VALUE`), step, run, state and activity events, a `cancelled` run outcome, `TokenUsage`, the request
  `RunAgentInput` (thread id, run id, messages, tools, state), and messages of the roles developer, system, assistant, user, tool,
  activity and reasoning. It has an SSE `EventEncoder`.
- The Vercel UI message stream is documented, with parts for start, text, reasoning, tool input and output, steps, finish, error and
  **abort**, an `[DONE]` end and the header `x-vercel-ai-ui-message-stream: v1`.
- This repository has `langchain` 1.4.9 and `langgraph` 1.2.19. A LangGraph graph's `astream` accepts the `messages` and
  `updates` modes, among others. `AIMessage.content_blocks` gives provider-independent content blocks. A Claude `thinking`
  block comes out as a `non_standard` block in a test with a hand-made chunk.
- Pydantic AI ships adapters for AG-UI and the Vercel protocol over one internal event stream. We copy the idea, with AG-UI's
  events as that stream.

**Still to check while building** (it needs the real packages, and some of it a live call, which is asked for first):
what the three providers return for thoughts through `content_blocks` and whether `show_thoughts` maps to the right option
for each; whether `astream`'s `messages` mode gives complete tool call arguments in time for `TOOL_CALL_END`; and the exact
`UIMessage` shape for tool parts in a request.

## Tests

No live model call in the unit tests: a fake streaming chat model in `tests/fakes.py`, and recorded chunk sequences.

- Mapping: text, thoughts then text, a tool call with streamed arguments, two tool calls, an error halfway, `show_thoughts` off;
  every sequence valid against the protocol's ordering.
- Input: system and developer rejected, the last message must be the user's, `run()` and `stream()` refuse the wrong agent.
- Messages: round trips both ways with tool calls, results, errors, reasoning with an encrypted value.
- Cancel: during text, a tool call's arguments, a running tool, the wait for a slot, through `runner.cancel`, and by dropping the
  iterator; after each, the row is `CANCELLED`, the slot is free and `new_messages` is valid to send back.
- Run log: the statuses, `thread_id`, no reuse, tokens.
- Adapters: a request body from each protocol's docs; the Vercel output equals the protocol page's examples.
- One live smoke check per provider only on request, with the cheapest model.

## The work

| Issue | |
|---|---|
| [#29](https://github.com/aadhithya/micro/issues/29) | The epic |
| [#30](https://github.com/aadhithya/micro/issues/30) | `StreamingAgent`, `ChatInput`, `runner.stream()`, the run log |
| [#31](https://github.com/aadhithya/micro/issues/31) | LangChain's stream to AG-UI events, and `show_thoughts` |
| [#32](https://github.com/aadhithya/micro/issues/32) | Messages in and out, converters, `new_messages` |
| [#33](https://github.com/aadhithya/micro/issues/33) | Stopping a response, and keeping the partial turn |
| [#34](https://github.com/aadhithya/micro/issues/34) | The AG-UI adapter |
| [#35](https://github.com/aadhithya/micro/issues/35) | The Vercel AI SDK adapter |
| [#36](https://github.com/aadhithya/micro/issues/36) | An example chat app, the guide and the skill reference |
