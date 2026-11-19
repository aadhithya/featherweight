# Spec: the agent runner

Status: implemented. How to use it is in [agents.md](../agents.md) and [launching-agents.md](../launching-agents.md). The
code is [runner.py](../../src/micro/runner.py).

- [Goal](#goal)
- [Running](#running)
- [The result](#the-result)
- [Attempts and retries](#attempts-and-retries)
- [Concurrency](#concurrency)
- [Launching agents](#launching-agents)
- [Tools and connections](#tools-and-connections)
- [Configuration](#configuration)
- [Logging](#logging)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)

## Goal

`AgentRunner` runs [agent definitions](agents.md): it validates, calls the model, checks what came back, retries, limits
concurrency, lets an agent launch other agents, and records every attempt ([jobs and tracing](jobs-and-tracing.md)). It
is not a workflow engine. Order, loops, branching and when to stop are the caller's code. A runner is not tied to one
agent: the agent is chosen on each call, and one runner can run any number of different agents.

## Running

```python
result = await runner.run(AgentClass, agent_input, job_id=job_id)
results = await runner.run_many(AgentClass, inputs, job_id=job_id)
```

- `run` takes the **class** and an input that is an instance of its `input_model`.
- `run_many` runs one agent over many inputs at the same time and returns the results **in input order**. It has no limit
  of its own: the concurrency limit bounds it.
- `job_id` is optional. Runs of one pipeline share a job. Without one, the runs have no job and no run store.
- **Runtime failures are values.** `run` returns a `FAILED` result. It does not raise.
- **Programming errors raise:** an input of the wrong type (`TypeError`), a `job_id` that does not exist.

## The result

`AgentResult[OutT]`: `status` (`SUCCESS` or `FAILED` only), `output` (the validated output model, on success), `error`
(the last error, on failure), `agent_run_id` (the last attempt), `input_tokens`, `output_tokens` (thinking included),
`attempts`, `latency_ms` (across attempts) and `model_name` (as the API reported it, which can differ from the one
requested). Tokens are summed over the model calls of the successful attempt. The tool results the model read are in its
input tokens. A failed result has no tokens.

## Attempts and retries

Each attempt is one row in the run log, and goes through these steps:

1. The tools of the attempt are collected, and MCP servers are started if needed ([below](#tools-and-connections)). The
   row is created as `INIT`.
2. The attempt waits for a concurrency slot. Then the row becomes `RUNNING`.
3. The chat model is built (a failure here is a setup error), the agent is run through langgraph, the structured reply is
   validated, and `check_output` runs.
4. The row is finished as `SUCCESS` with the output, or `FAILED` with the error.

**Retries.** A failed attempt is retried, up to `max_attempts` (3). The wait between attempts is `retry_delay_s` (1s),
and it does not hold a concurrency slot.

- A retry **resamples with the same input**. The error is recorded in the log but is not sent back to the model. If a
  retry should know what went wrong, the pipeline does that: it builds a new input.
- Failed attempts: an API error, a reply that does not validate, no structured reply, a rejection by `check_output`, the
  recursion limit, and any other exception.
- **Not retried** (the run fails at once): a `SetupError` (no API key, an unknown model, an MCP server that cannot start,
  two tools with one name), and any 4xx response except 408, 409 and 429, found on the exception or its causes.
- A `FAILED` result carries the last attempt's error, with its type, at most 2,000 characters.

## Concurrency

A semaphore of `max_concurrent` slots (default 10) bounds the attempts that run at once on one runner, across every
agent. A queued attempt shows as `INIT` until it holds a slot. The slot is released between attempts.

## Launching agents

An agent can hand part of its work to another agent: it launches it, waits, and reads the result.

**Declaration.** `subagents` is an allow-list of agent **classes**: the agent can launch exactly those. There is no
registry to maintain. The agent definition checks the list ([agents](agents.md#checked-when-an-agent-is-built)).

**What the model gets**, for an agent with sub-agents and depth left (`can_launch = bool(subagents) and depth <
max_depth`):

- the prompt lists the agents it may launch, by name and description;
- two tools built for each attempt: `get_agent_schema(agent_name)`, which returns the agent's name, description and
  input and output JSON schemas, and `launch_agent(agent_name, input_json)`, which validates `input_json` against the
  child's input model, runs the child to the end and returns the result.

`input_json` is a **string**, because Gemini can reject a function parameter that is an object with no declared
properties (checked with the real model).

**Both tools always return text and never raise into the parent's run**, so that the model can read what happened:

| Case | What comes back |
|---|---|
| The child succeeds | `{"status": "SUCCESS", "agent": "...", "output": {...}}` |
| The child fails after its own retries | `{"status": "FAILED", "agent": "...", "error": "..."}` |
| A name that is not on the allow-list | `'x' is not an agent you can launch. You can launch: ...` |
| The input does not fit the child's input model | `The input for 'x' is not valid: ... Check get_agent_schema and try again.` |
| The depth or the launch limit is reached | A sentence saying which limit |

**The child is an ordinary run.** It has its own retries, tools, skills, MCP servers and sub-agents, and its own rows in
the log. It shares the parent's job, so it shares the run store. Its row records `parent_agent_run_id` (the attempt that
launched it) and `depth` (1 for a top-level run). The parent sees only the child's output, as a tool result, which counts
in the parent's input tokens. A model may launch several agents in one turn, and they run at the same time. A retried
parent attempt launches its children again, and the earlier children stay in the log under the failed attempt.

**Limits.**

- **Depth** (`max_depth`, default 3): the most agents in one chain, the first included. A depth of 1 turns launching
  off. At the limit an agent gets no launch tools and no mention of them in its prompt, and the tool refuses a call made
  anyway.
- **Breadth** (`max_launches`, default 10): the most launches one attempt of one agent may make. Launches that fail
  validation do not count. It is per attempt, not per job, so with the defaults a tree can reach 1 + 10 + 100 = 111
  agents before retries. This is different from `recursion_limit`, which bounds the steps of one agent's own tool loop.

**The slot hand-over.** A parent that waits for its child holds a slot the child needs, and with enough waiting parents
nothing could finish. So a parent gives up its slot while its launches run and takes one again when the last of them is
done, also when it launched several at once. The limit on model calls at one time still holds, because a waiting parent
is not calling the model.

## Tools and connections

Each attempt builds a [tool registry](tools-and-store.md#the-tool-registry) from the agent's `tools`, the tools of its MCP
servers, and the tools the runner adds: `launch_agent` and `get_agent_schema` (with sub-agents and depth left) and
`get_skill` (with skills) and `search_tools` and `call_tool` (with an MCP server that is found by search). A failure to build
it is a setup error.

**MCP connections** are the runner's, shared by every agent it runs ([MCP tools](mcp-tools.md)). A run holds a lease on
its servers from its first attempt to its last, so a retry does not start a server again, and lets go when it is done. A
server stops when its last lease is released. `async with AgentRunner(...)`, or `await runner.aclose()`, is optional: it
keeps servers up between runs and stops them at the end. A runner with no MCP agents needs neither.

## Configuration

| Argument | Environment variable | Default | |
|---|---|---|---|
| `max_attempts` | | 3 | At least 1 |
| `retry_delay_s` | | 1.0 | |
| `store` | | a new `RunStore` | What the store tools read and write |
| `max_concurrent` | `MAX_CONCURRENT_AGENT_RUNS` | 10 | At least 1 |
| `max_depth` | `MAX_AGENT_DEPTH` | 3 | At least 1 |
| `max_launches` | `MAX_AGENT_LAUNCHES` | 10 | At least 1 |

An argument wins over the variable. A value below 1, or an environment variable that is not an integer, raises. The key of the provider (`GOOGLE_API_KEY`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`)
is needed for the real model. A missing key is a setup error: the run fails at once, without retries.

## Logging

`invocation_started` and `invocation_finished` (with attempts, tokens and latency), `attempt_started`, `attempt_failed`
(with `non_retryable`), `retry_waiting`, `waiting_for_slot` and `slot_acquired`, `fan_out_started` and `fan_out_finished`,
`agent_launch` (parent, child, depth and launch count), `skill_read`, and the `mcp_*` lines. All are plain
`key=value` text through `logging`.

## Decisions

- **The runner does not choose the model.** The agent's `model` settings are the only way to pick one, and the runner calls
  `agent.model.build()`. A test replaces the settings with fake ones, so tests take the production path
  ([models](models.md#testing)).
- **Failures are values.** A pipeline decides what a failed run means: skip a document, stop the job, ask again.
- **A retry resamples, and does not feed the error back.** Feeding it back is a different feature, and the pipeline can do
  it by building a new input.
- **Allow-lists of classes**, so that there is nothing to register and a rename is a normal refactor.
- **The child's result is text for the parent**, not an exception, so that the parent's model can recover.
- **Depth and breadth, not cost.** They are simple to explain and to set. A budget is not built.
- **Slots are held by attempts that call the model**, which keeps the limit on model calls honest.
- **The runner owns no workflow.** It is not a graph engine, and it does not resume.

## Not supported

- **Finding agents.** An agent knows only its own allow-list. There is no tool to search for agents.
- **Launching in the background.** `launch_agent` waits for the child.
- **Resuming** a run or a launch, or reusing the result of an earlier launch when a parent is retried.
- **A limit on launches per job**, or on cost or tokens.
- **Cancelling one run.** Cancelling the task that awaits it cancels the run.
- **A runner shared between processes.** The slots and the connections are per runner.

## Tests

[test_runner.py](../../tests/test_runner.py) (results, retries, non-retryable errors, token sums),
[test_limits.py](../../tests/test_limits.py) (the three limits: defaults, environment variables, arguments, and the slot
limit with `run_many`),
[test_concurrency.py](../../tests/test_concurrency.py) (many runs at once do not interfere),
[test_subagents.py](../../tests/test_subagents.py) (the allow-list, the two tools, depth and breadth, the slot hand-over
with deadlock tests, tracing), and [test_mcp.py](../../tests/test_mcp.py) (leases). A real-model test of launching is in
[tests/e2e/test_subagents_e2e.py](../../tests/e2e/test_subagents_e2e.py).
