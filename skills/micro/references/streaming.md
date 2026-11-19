<!-- Copied from docs/streaming.md by scripts/build_skill.py. Edit the original, not this file. -->

# Streaming agents

An agent that talks to a person while it works: a chat, or any task someone watches. The answer arrives as the model writes it,
as events, and nothing is validated at the end.

> **Status: being built** ([epic #29](https://github.com/aadhithya/micro/issues/29)). What works today is on this page. The
> design is in the [spec](https://github.com/aadhithya/micro/blob/main/docs/specs/streaming.md), which also lists what is still to come: the full set of events (tool calls and
> thoughts), tool calls in the history, stopping a response, and the AG-UI and Vercel AI SDK adapters.

- [A streaming agent](#a-streaming-agent)
- [Streaming a turn](#streaming-a-turn)
- [The conversation is yours](#the-conversation-is-yours)
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
| `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT` (one per chunk), `TEXT_MESSAGE_END` | The model writes text. A message is closed when the model turn changes or the run ends |
| `RUN_FINISHED` | Last, on success: `outcome` is `success`, and `usage` has the tokens |
| `RUN_ERROR` | Last, on failure. Whatever was open is closed first |

When the loop ends, `run.status` is `SUCCESS` or `FAILED`, `run.error` says why, and `run.usage` has the tokens.
`run.run_id` is the id of the run in the [run log](#what-is-logged).

To stop early, leave the loop and close the run (`contextlib.aclosing(run)` does it for you). The run is stopped, its row is
`FAILED` ("Stopped before it finished"), and the slot is given back. A proper stop that keeps the partial turn comes with
[the next parts of the epic](https://github.com/aadhithya/micro/blob/main/docs/specs/streaming.md#stopping-a-response).

`stream()` raises for a mistake in your code: an agent that is not a `StreamingAgent`, an input that is not a `ChatInput`
(`TypeError`), or a `job_id` that does not exist (raised when the loop starts). A `StreamingAgent` passed to `runner.run()` is a
`TypeError` too. Failures at run time (a provider that is down) are events, not exceptions.

## The conversation is yours

`micro` keeps no conversation. A `ChatInput` is the whole conversation so far, and the agent sees only that:

- the **last message is from the user**;
- **`system` and `developer` messages are refused**: the agent has its own prompt, and a client must not be able to replace it;
- `thread_id` is optional. It names the conversation in the run log. It does not load anything.

For now the history may have text from the user and the assistant only. Tool calls, tool results and thoughts in the history
come with the messages part of the epic, together with converters to and from LangChain messages, so that you can keep the
conversation in any LangChain chat message store.

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

Tracked in [epic #29](https://github.com/aadhithya/micro/issues/29): tool call and thought events (#31), messages in and out
(#32), stopping a response (#33), the AG-UI adapter (#34), the Vercel AI SDK adapter (#35), an example and the full guide (#36).
Background agents, jobs across processes and stored conversations are not part of it.
