# Configuration

Status: implemented, in [issue #231](https://github.com/aadhithya/featherweight/issues/231). How to use it is in [the guide](../agents.md#configuration). The code is [config.py](../../src/featherweight/config.py).

The numbers that tune a whole deployment, and where they are set. What each one does is in the spec that owns the thing it tunes ([the runner](runner.md), [submitted jobs](submitted-jobs.md), [skills](skills.md), [observers](observability.md)).

- [Goal](#goal)
- [The rule](#the-rule)
- [The settings](#the-settings)
- [What is not configuration](#what-is-not-configuration)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)

## Goal

A setting that applies to a whole deployment is set where a deployment is configured: the environment. One that you also change by hand, while you try something out, can be passed in code. And there is
**one list**: every variable featherweight reads has the prefix `FTW_`, is in the table below, and is read by one function, so there is no variable that you find only by reading the code.

## The rule

- **Every environment variable of featherweight starts with `FTW_`.** (The keys of the model providers, the names that an MCP server's `env_from` and `headers_from` point to, and OpenTelemetry's own
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT` are not featherweight's, and keep their names.)
- **Two kinds of setting.**
  - **Both ways:** the argument of the constructor wins; if there is none, the variable; if there is none, the default. These are the numbers you change by hand when you try something out: how many attempts
    run at once, how far agents launch agents, how many jobs a worker runs at once.
  - **Environment only:** the variable, else the default. There is no constructor argument. These are the numbers that tune a platform and that nobody sets for one worker: leases, timeouts, polling.
- **Read when the object is built,** not at import, and kept as an attribute. A test sets the variable, then builds the runner or the worker. A change of the variable later does not reach an object that exists.
- **A variable that is set but empty counts as not set,** for a number and for `FTW_LOG_LEVEL`. A compose file or a `.env` hands over an empty value when you chose none (`${FTW_LOG_LEVEL:-}`).
- **A value that is not a positive number raises `ValueError`** that names the variable (or the argument), when the object is built. A count must be an integer. There is **no way to switch a timeout off**: a very large value does it.
- **The effective values are logged** at `INFO` when a runner or a worker is built (`runner_configured`, `worker_configured`), so that a setting that came from the environment is not invisible.

## The settings

| Variable | Default | Argument | What it sets |
|---|---|---|---|
| `FTW_MAX_CONCURRENT_ATTEMPTS` | 10 | `AgentRunner(max_concurrent=)` | Model attempts running at once, per runner ([runner](runner.md#concurrency)) |
| `FTW_MAX_AGENT_DEPTH` | 3 | `AgentRunner(max_depth=)` | Most agents in one chain of launches. 1 turns launching off |
| `FTW_MAX_AGENT_LAUNCHES` | 10 | `AgentRunner(max_launches=)` | Most launches per attempt of one agent |
| `FTW_MAX_BACKGROUND_JOBS` | 3 | `AgentRunner(max_background_jobs=)` | Most children one attempt may have running in the background |
| `FTW_WORKER_CONCURRENCY` | 10 | `JobWorker(concurrency=)` | Jobs one worker runs at once |
| `FTW_OBSERVER_TIMEOUT` | 1 s | none | How long an observer hook may take before the runner goes on ([observers](observability.md)) |
| `FTW_WORKER_RECEIVE_TIMEOUT` | 1 s | none | How long a worker waits for a message before it looks again whether it is stopping |
| `FTW_WORKER_CANCEL_POLL` | 1 s | none | How often a worker asks whether a job it runs was cancelled in another process |
| `FTW_JOB_LEASE` | 60 s | none | How long a worker holds a job without renewing it, before another may take it over ([submitted jobs](submitted-jobs.md#several-workers)) |
| `FTW_HEARTBEAT` | a third of the job's lease | none | How often a worker renews the leases of its jobs and of their messages |
| `FTW_QUEUE_LEASE` | 60 s | none | How long a message that was received stays invisible to other receivers, in the queues featherweight ships (`SqliteQueue`) |
| `FTW_QUEUE_MAX_DELIVERIES` | 3 | none | How many times `SqliteQueue` delivers a message before it marks it `dead` |
| `FTW_JOBS_LOOKUP_TIMEOUT` | 15 s | none | How long `jobs_lookup` may take ([submitted jobs](submitted-jobs.md#jobs-that-are-not-declared-in-code)) |
| `FTW_SKILL_READ_TIMEOUT` | 15 s | none | How long a skill's `aread` may take ([skills](skills.md#reading-a-skill-from-somewhere-else)) |
| `FTW_DB_PATH` | `featherweight.db` | the path given to `SqliteDatabase` | The sqlite file |
| `FTW_LOG_LEVEL` | `INFO` | the level given to `setup_logging()` | The log level |

`FTW_DB_PATH` and `FTW_LOG_LEVEL` are not numbers and are read where they were, by `SqliteDatabase` and `setup_logging()`: an argument wins there too.

## What is not configuration

- **How long this call waits** is an argument of the call: `JobClient.wait(timeout)`, `JobQueue.receive(timeout)`, `JobWorker.stop(grace)`.
- **A timeout that belongs to a typed definition** differs per job or per server, so it is in the definition: a job's `timeout`, an MCP server's `timeout_s`.
- **The attachment limits** (`max_attachments`, `max_bytes`) are class attributes of the streaming agent: they differ per agent, so they are in the definition ([attachments](attachments.md#limits)). There is no variable for them and no runner argument.
- **A runner's own behaviour in code:** `max_attempts` and `retry_delay_s` stay arguments of `AgentRunner`.
- **`MemoryQueue(lease=)`** (in `featherweight.testing`, for tests) is an argument: [the queue kit](submitted-jobs.md#the-queue) builds queues with `make_queue(lease)`, and a queue that someone else writes reads its own setting.

## Decisions

- **One list and one reader.** Before this, some numbers were variables, some were arguments and the limits were both; that made each one a thing to look up. The list is the table above, and `featherweight.config` reads it.
- **Both ways only where a person changes it by hand.** A notebook or a local run should not need an environment to try `max_depth=1`; nobody sets a lease for one worker, so a lease has no argument and the constructors do not grow
  a parameter for every knob.
- **The `FTW_` prefix on every variable,** so that a bare name cannot collide with another tool's in a deployment.
- **Two leases, two variables.** The job's lease and the queue's lease have different jobs ([submitted jobs](submitted-jobs.md#several-workers)) and are set apart.
- **Read once, when built.** A value that changed under a running worker would be a cause of failures that cannot be reproduced.
- **No off switch for a timeout,** because a timeout that can be turned off is the one that is off when it is needed.
- **Pre-release: no alias.** The old names (`MAX_CONCURRENT_ATTEMPTS`, `MAX_AGENT_DEPTH`, `MAX_AGENT_LAUNCHES`) and the removed arguments are gone.

## Not supported

- A configuration file, and a settings object that is passed around.
- Changing a setting of a running runner or worker.
- A way to switch a timeout off.

## Tests

- For each setting: the default; the variable; for a "both ways" setting, that the argument wins; a value that is not a number, is zero or is negative raises `ValueError` that names the variable; an object built before the variable was set does not see it.
- The table here, the list in `featherweight.config` and the Configuration table of [the guide](../agents.md#configuration) say the same (a test compares them).
- The runner and the worker log their effective values when they are built.
- The old names do nothing.
