<!-- Copied from docs/launching-agents.md by scripts/build_skill.py. Edit the original, not this file. -->

# Agents that launch agents

An agent can hand part of its work to another agent: it launches it, waits, and reads the result. This page says how
that is declared, what the model sees, what is limited, and how it is traced. Defining agents is in
[agents.md](agents.md).

- [Declaring it](#declaring-it)
- [What the model sees](#what-the-model-sees)
- [Behaviour](#behaviour)
- [Limits](#limits)
- [Concurrency](#concurrency)
- [Tracing](#tracing)
- [What is not supported](#what-is-not-supported)

To see it run: [`examples/launching`](https://github.com/aadhithya/micro/blob/main/examples/launching) is an `Editor` that may launch a summariser and a
translator (`make example-launching`). It prints the runs of each request as a tree.

## Declaring it

```python
class Researcher(AgentBase):
    name: str = "researcher"
    description: str = "Finds facts about a topic"  # this is what the parent's model reads
    ...


class Planner(AgentBase):
    ...
    subagents: list[type] = [Researcher, Writer]  # the only agents Planner can launch
```

`subagents` holds agent **classes**. It is the allow-list: an agent can launch exactly those, and nothing else.
There is no global registry, so there is nothing to register and a rename is a normal refactor.

Checked when the agent is built (a programming error that raises, not a failed run):
- every entry is an `AgentBase` subclass;
- an agent cannot list **its own class**;
- the names of its sub-agents are distinct.

Agent A listing B while B lists A is allowed; the depth limit stops it.

## What the model sees

An agent with sub-agents, and depth left to launch, gets two additions.

**In the system prompt:** the agents it may launch, by name and description, and how to use them. Not their schemas.

```
You can launch other agents to do part of the work. You may launch only these agents:
- researcher: Finds facts about a topic
- writer: Writes a short text from facts
Before you launch an agent, call `get_agent_schema` ...
```

**Two tools**, built by the runner for each attempt:

| Tool | |
|---|---|
| `get_agent_schema(agent_name)` | The agent's name, description, `input_schema` and `output_schema` (JSON schemas), as JSON. The model asks for this when it needs an agent's input, so the prompt stays small |
| `launch_agent(agent_name, input_json)` | Validates `input_json` (the input as a JSON **string**) against the agent's input model, runs the agent to the end and returns the result |

The input is a string, not an object, because Gemini function declarations can reject an object parameter that has no
declared properties. It was checked with the real model.

## Behaviour

`launch_agent` always returns text, so the model can read what happened and carry on. It never raises into the parent's
run.

| Case | What comes back |
|---|---|
| The child succeeds | `{"status": "SUCCESS", "agent": "...", "output": {...}}`, the child's output as JSON |
| The child fails (after its own retries) | `{"status": "FAILED", "agent": "...", "error": "..."}` |
| A name that is not on the allow-list | `'x' is not an agent you can launch. You can launch: ...` |
| The input does not fit the child's input model | `The input for 'x' is not valid: <the validation errors>. Check get_agent_schema and try again.` |
| The depth or the launch limit is reached | A sentence saying which limit |

- **The child is an ordinary run.** It has its own retries, its own tools, its own rows in the log, and its own
  `skills` and `subagents`.
- **The child shares the job**, so it shares the run store. State passed with `store_set` and `store_get` is visible to
  both.
- **The result is the child's output only.** The parent reads it as a tool result, so it counts in the parent's input
  tokens on its next model call. The child's own tokens are in its own rows.
- **A model may launch several agents in one turn.** They run at the same time.
- **A retried parent attempt launches its children again.** The earlier children stay in the log, under the failed
  attempt that launched them.

## Limits

| Limit | Default | Set with | |
|---|---|---|---|
| Depth | 3 | `MAX_AGENT_DEPTH`, or `AgentRunner(max_depth=...)` | The most agents in one chain, the first one included. 3 means A launches B launches C, and C cannot launch |
| Breadth | 10 | `MAX_AGENT_LAUNCHES`, or `AgentRunner(max_launches=...)` | The most launches one attempt of one agent may make |

- A depth of 1 turns launching off.
- **At the depth limit** an agent is given no launch tools and no mention of them in its prompt. The tool also refuses
  a call made anyway.
- **Invalid launches do not count** against the breadth limit; only launches that ran do.
- The breadth limit is per attempt of one agent, not per job, so a tree is bounded but can be wide: with the defaults, up
  to 1 + 10 + 100 = 111 agents in one tree, before retries. Lower `MAX_AGENT_LAUNCHES` or `MAX_AGENT_DEPTH` if that is
  too many for your budget.
- These are different from `recursion_limit` on an agent, which only bounds the steps of one run's own tool loop.

## Concurrency

Every attempt holds one concurrency slot while it runs (`MAX_CONCURRENT_AGENT_RUNS`). A parent waiting for its child
would hold a slot the child needs, and with enough waiting parents nothing could finish. So **a parent gives up its
slot while its launches run, and takes one again when the last of them is done**, also when it launched several at
once. The limit on model calls at one time still holds, because a waiting parent is not calling the model.

## Tracing

Children are in the same job. Each row in `agent_runs` records:

- `parent_agent_run_id`: the attempt that launched it (empty for a top-level run);
- `depth`: 1 for a top-level run, 2 for the agent it launched, and so on.

The whole tree of a job is one query:

```
sqlite3 micro.db "select depth, agent_name, attempt, status, parent_agent_run_id from agent_runs where job_id = '<job id>' order by rowid"
```

The log has one line per launch: `agent_launch parent_agent_run_id=... child=... depth=2 launch=1/10`.

The runner records the tools an agent had, `launch_agent` and `get_agent_schema` among them, in `tool_names`.

## What is not supported

- **Finding agents.** An agent knows only its own allow-list. There is no tool to search for agents.
- **Launching in the background.** `launch_agent` waits for the child.
- **Reusing a launch** when a job is resumed. A parent that succeeded is reused whole; one that failed runs again with
  its children ([resuming a job](agents.md#resuming-a-job)).
- **A limit on the number of launches per job**, or on cost. Depth and breadth are the only limits.
