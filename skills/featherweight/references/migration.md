<!-- Copied from docs/migration.md by scripts/build_skill.py. Edit the original, not this file. -->

# Migrating from 0.1

Everything that breaks between 0.1 and the next release, what to search for in your project, what to write instead, and how to check. It is for people, and for the coding agents that work on projects that use featherweight: the last section is written for them, and the skill carries the same page.

The rename of the units is [specified](https://github.com/aadhithya/featherweight/blob/main/docs/specs/units.md), and a test checks the names of its tables against the code (the Python, SQL, settings, spans and logs, and the steps for coding agents).

- [What breaks, in the order you are likely to meet it](#what-breaks)
- [The run store is gone](#the-run-store-is-gone)
- [`background_agents` is `background_jobs`](#background_agents-is-background_jobs)
- [Runs, attempts and executions](#runs-attempts-and-executions)
- [A `Database` you wrote](#a-database-you-wrote)
- [A queue you wrote](#a-queue-you-wrote)
- [Stricter checks: identity, job names and skills](#stricter-checks-identity-job-names-and-skills)
- [Attachments in a chat](#attachments-in-a-chat)
- [Settings, logs and spans](#settings-logs-and-spans)
- [Your database upgrades by itself](#your-database-upgrades-by-itself)
- [What is new, and does not need a change](#what-is-new)
- [For coding agents](#for-coding-agents)

## What breaks

| # | Change | You are affected if you... |
|---|---|---|
| 1 | [The run store is gone](#the-run-store-is-gone) | use `RunStore`, `AgentRunner(store=)`, `RunContext.store` or the tools `store_get` and `store_set` |
| 2 | [`background_agents` is `background_jobs`](#background_agents-is-background_jobs) | give an agent `background_agents` |
| 3 | [`agent_run_id` is `attempt_id`, and the other words](#runs-attempts-and-executions) | read `result.agent_run_id`, use the run log's methods, rows or table, or implement observers |
| 4 | [A `Database` you wrote](#a-database-you-wrote) | implemented `Database` yourself |
| 5 | [A queue you wrote](#a-queue-you-wrote) | implemented `JobQueue` yourself |
| 6 | [Stricter checks: identity, job names and skills](#stricter-checks-identity-job-names-and-skills) | pass one `job_id` to runs for several identities, declare an identity model with an `exclude=True` field and no default, name jobs freely, or write a skill's `aread` |
| 7 | [Attachments in a chat](#attachments-in-a-chat) | send an empty attachment, match the text of an error, or catch `ValueError` around `runner.stream()` |
| 8 | [Settings, logs and spans](#settings-logs-and-spans) | set `MAX_CONCURRENT_AGENT_RUNS`, read the logs, or have dashboards or alerts on the spans |

If you use none of these, upgrade and run your tests.

## The run store is gone

Agents do not share state: information moves through outputs and return values. The in-memory store (one per runner and process) could not be shared by workers anyway.

| Gone | Instead |
|---|---|
| `RunStore`, `from featherweight import RunStore` | Keep state in your own store, or in a dict keyed by the job id when it lives in one process |
| `AgentRunner(db, store=...)` | Drop the argument |
| `RunContext.store` | Drop the use |
| the tools `store_get` and `store_set` | Drop them from `tools`, and from prompts that name them |
| the `fakeredis` dependency | Nothing to do |

Search: `RunStore`, `store_get`, `store_set`, `store=`, `.store`. Check: your tests pass, and `git grep -n "store_get\|store_set\|RunStore"` is empty.

## `background_agents` is `background_jobs`

The field of an agent takes agent classes and `Job` classes, and an agent class in it runs as a job of its own, so the name says what it holds.

```python
# before
class Researcher(AgentBase):
    background_agents = [WebSearcher, Summariser]


# after
class Researcher(AgentBase):
    background_jobs = [WebSearcher, Summariser]
```

There is no alias. The tool `start_background` and the events `background_started` and `background_finished` keep their names. **The agent's hash does not change**, so a resume of a job made before the upgrade still reuses its steps.
Search: `background_agents`. Check: `git grep -n background_agents` is empty.

## Runs, attempts and executions

Each word now names one unit ([the spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/units.md)): a **run** is one call of an agent, with its retries; an **attempt** is one try within a run; an **execution** is one start of a job by a worker; a **job** is a declared pipeline. "Invocation" is retired, and the id that was called
`agent_run_id` was an attempt's, so it is `attempt_id`. The table:

**Python**

| Before | After |
|---|---|
| `result.agent_run_id` | `result.attempt_id` (and `result.run_id` is new: the id of the run) |
| `runtime.context.agent_run_id` (in a tool) | `runtime.context.attempt_id` (and `run_id`) |
| `CURRENT_AGENT_RUN_ID` | `CURRENT_ATTEMPT_ID` |
| `db.list_runs(job_id=..., invocation_id=...)` | `db.list_attempts(job_id=..., run_id=...)` |
| `db.get_run`, `create_run`, `mark_run_running`, `finish_run` | `db.get_attempt`, `create_attempt`, `start_attempt`, `finish_attempt` |
| `db.find_reusable_run(job_id, agent_hash, input_hash, identity_hash)` | `db.find_reusable_attempt(...)` |
| `AgentRunRow` | `AttemptRow` |
| `RunStart`, `RunEnd` (in `featherweight.db`, and exported from `featherweight`) | `AttemptRecord`, `AttemptFinish` |
| `row.agent_run_id`, `row.parent_agent_run_id`, `row.reused_from_agent_run_id`, `row.invocation_id` | `row.attempt_id`, `row.parent_attempt_id`, `row.reused_from_attempt_id`, `row.run_id` |
| `job_row.parent_agent_run_id` | `job_row.parent_attempt_id` |
| the same fields of the observers' records | `attempt_id`, `run_id`, `parent_attempt_id`, `reused_from_attempt_id` |
| `AgentRunFailed` | `RunFailed` |
| `RunStatus` | `Status` (same members: `INIT`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED`) |
| `ReusedRun` (observer record) | `RunReused` |
| `AgentRunner(max_background=...)` | `AgentRunner(max_background_jobs=...)` |

**A streamed run** (`runner.stream`): `run.run_id` is the id of the **run**, the one that its events carry and that `runner.cancel` takes, and the row of the run log has its own id, `run.attempt_id`. It used to be one id. So `await db.get_attempt(run.run_id)` (the old `db.get_run(run.run_id)`) now returns `None`, with no error: look the row up with `get_attempt(run.attempt_id)`, or with `list_attempts(run_id=run.run_id)` (its `run_id` column is the run's).

**SQL**, if you query the run log yourself: the table `agent_runs` is `attempts`; the columns `agent_run_id`, `invocation_id`, `parent_agent_run_id` (also in `jobs`) and `reused_from_agent_run_id` are `attempt_id`, `run_id`, `parent_attempt_id` and `reused_from_attempt_id`.
The database is migrated for you: [see below](#your-database-upgrades-by-itself).

Check: the old names are gone from your code (`git grep -n "agent_run_id\|invocation_id\|list_runs\|AgentRunRow"` is empty), and your tests pass.

## A `Database` you wrote

- Your methods are renamed as in the table above (`create_attempt`, `start_attempt`, `finish_attempt`, `get_attempt`, `list_attempts`, `find_reusable_attempt`), and the records and rows too.
- `find_reusable_attempt` takes `identity_hash` ([who is asking](agents.md#who-is-asking-the-identity-of-a-run)): a step is reused only when the old attempt's `identity_hash` equals the new one, and `None` matches only `None`. The attempt record has an `identity_hash` field.
- Your schema has the table `attempts` with the renamed columns and a nullable `identity_hash` column.
- It has a new abstract method, `job_has_other_identity(job_id, identity_hash)`: whether the job has an attempt whose identity is not the one given (a job is for one identity). A `Database` that lacks it cannot be built.
- Run `testing.check_database` against it, which checks all of this.

## A queue you wrote

A message has two new fields, `identity` (a JSON mapping or `None`) and `identity_kind` (the name of the identity's class, or `None`): carry them unchanged. Your queue stores them with the other fields, and returns them. `testing.check_queue` checks that it does, and a queue that drops either fails. `SqliteQueue(db)` takes no `lease` or `max_deliveries` any more (they are `FTW_QUEUE_LEASE` and `FTW_QUEUE_MAX_DELIVERIES`); `testing.MemoryQueue(lease=)` keeps its argument.

## Stricter checks: identity, job names and skills

These were accepted before, or failed late. Now they are refused where they are made. Each is a change of code or of a data model, not a rename.

| What | Now | You are affected if you... | Do |
|---|---|---|---|
| A **job is for one identity** | A run that names a `job_id` raises `ValueError` ("a job is for one identity") when the job already has runs for another identity (or for none, and the reverse) | pass one `job_id` to runs for several users | Use one job id for each identity |
| `identity_hash` covers the **class name** of the identity | Hashes recorded before differ, so a resume of a job that was resumable and in flight reruns its steps once | resume jobs across the upgrade | Nothing: the steps run again, once |
| An identity model with a field marked `exclude=True` **and no default** | `TypeError` when the runner, the worker or the client is built (a worker rebuilds the identity without that field) | have such a field, for a token that stays in the process | Give the field a default (`None`) |
| A union of identity models with **two classes of one name** | Refused when built (the class name travels in the queue) | name two identity classes alike | Rename one |
| `JobWorker(identity_type=...)` | `TypeError` when built if its runner does not accept that type | give a worker and its runner different identity types | Use one type |
| `run_many([])` | Checks the identity, as a call with inputs does (`TypeError` if it is missing or of the wrong type) | call it with an empty list and no identity | Pass the identity |
| A **job name** | Must match `^[A-Za-z][A-Za-z0-9_.-]*$` in full: a name with a newline at the end is refused (`TypeError` for a class). A message whose name does not match fails with `UnknownJob`, and `jobs_lookup` is not asked | build job names from user input | Choose the names yourself |
| `jobs_lookup` | Cancelled after `FTW_JOBS_LOOKUP_TIMEOUT` (15 s); the job fails with `JobLookupError` | have a lookup that can take longer | Make it quicker, or set the variable |
| `get_skill` with a **resource path** | Refused when it has `..`, starts with `/`, has a backslash or a control character, or is over 256 characters; `./` and `//` are tidied. `Skill.read` and `aread` never see a refused path | have a skill that is read with such a path | Use plain relative paths |
| `Skill.aread` | Must return text: `None` or `bytes` is a failed read. Cancelled after `FTW_SKILL_READ_TIMEOUT` (15 s) | wrote an `aread` | Return `str`; keep it quick |

Check: your tests pass, and a worker starts (it builds its checks when it starts).

## Attachments in a chat

| What | Now | You are affected if you... | Do |
|---|---|---|---|
| An **empty attachment** (no bytes) | Refused when a `ChatInput` is built (`ValueError`, a `BadRequest` through an adapter). It used to fail at the provider, in the run | send one, or have a stored conversation that has one | Drop the part before you build the `ChatInput` |
| The **error for data that is not base64** | Names the likely cause: a `data:` URL, line breaks or spaces, the URL-safe alphabet, missing padding. The form that is taken is stated: what `base64.b64encode` and `btoa` give. Nothing that was taken is refused now, except the empty one | match the text of the error | Match the type (`ValueError`), not the text |
| The **limit error** | `AttachmentLimitError`, a subclass of `BadRequest` (so still a `ValueError`), from `runner.stream()`. A handler for `BadRequest` now answers it too (a 422); a handler for the subclass can answer 413 | catch `ValueError` around `runner.stream()`, or let it reach your framework (a 500 before) | Add a handler for `AttachmentLimitError` that answers 413, before the one for `BadRequest` |
| `BadRequest` | Defined in `featherweight.errors`. `featherweight.BadRequest`, `featherweight.adapters.BadRequest` and `featherweight.adapters.errors.BadRequest` are one class | import it from the old module | Nothing |
| The **messages of an adapter's `BadRequest`** | Both adapters word a refusal alike: `messages[0].content[0] is image content ...`, and a missing mime type reads `messages[0].content[0].source.mimeType: Field required`; the AG-UI adapter no longer says "Value error," or "body:" | match the text | Match the type |

A file is not carried from one agent to another, or through a queue, on purpose: [pass a reference](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/files-for-an-agent-or-a-job.md). The limits (`max_attachments` 10, `max_bytes` 15 MiB) have not changed.

## Settings, logs and spans

| Before | After | Notes |
|---|---|---|
| the environment variable `MAX_CONCURRENT_AGENT_RUNS` | `FTW_MAX_CONCURRENT_ATTEMPTS` | It limits the attempts (the model calls) that run at once. The argument `max_concurrent` is unchanged. |
| the environment variables `MAX_AGENT_DEPTH`, `MAX_AGENT_LAUNCHES` | `FTW_MAX_AGENT_DEPTH`, `FTW_MAX_AGENT_LAUNCHES` | Every variable of featherweight starts with `FTW_`. The old names do nothing. |
| the arguments `AgentRunner(observer_timeout=)`, `JobWorker(lease=, heartbeat=, receive_timeout=, cancel_poll=)`, `SqliteQueue(lease=, max_deliveries=)` | `FTW_OBSERVER_TIMEOUT`, `FTW_JOB_LEASE`, `FTW_HEARTBEAT`, `FTW_WORKER_RECEIVE_TIMEOUT`, `FTW_WORKER_CANCEL_POLL`, `FTW_QUEUE_LEASE`, `FTW_QUEUE_MAX_DELIVERIES` | These tune a deployment and are environment-only; passing the argument is a `TypeError`. [The list](https://github.com/aadhithya/featherweight/blob/main/docs/specs/configuration.md#the-settings). |
| the span attribute `ftw.invocation.id` | `ftw.run.id` | |
| the span attribute `ftw.run.id` | `ftw.attempt.id` | **`ftw.run.id` now means something else** (a run, not an attempt): change dashboards and alerts that read it before you upgrade the attribute they read. |
| the span attribute `ftw.reused_from_agent_run_id` | `ftw.reused_from_attempt_id` | |
| the log lines `invocation_started`, `invocation_finished` | `run_started`, `run_finished` | One per run. |
| the log lines `run_created`, `run_running`, `run_finished` (the run log's rows) | `attempt_created`, `attempt_running`, `attempt_finished` | One per attempt. Mind the order if you rewrite them by script: `run_finished` is both a before and an after. |
| the keys `agent_run_id=` and `invocation_id=` in the log lines | `attempt_id=` and `run_id=` | |
| the key `reused_from=` in the log lines of a resume | `reused_from_attempt_id=` | The three lines of a resume (`run_reused`, `run_reuse_rejected`, `result_missing`) also carry `run_id=` now. |

The span **names** do not change (`invoke_workflow`, `invoke_agent`, `chat`, `execute_tool`).

## Your database upgrades by itself

`SqliteDatabase` and `PostgresDatabase` apply the migrations they have not applied when they connect, in a transaction each, and keep every row:

| Migration | What it does |
|---|---|
| 2, `identity_hash` | Adds `agent_runs.identity_hash`. Runs from before it have none. |
| 3, `queue_identity` | Adds the column of the identity to the sqlite queue table. |
| 4, `attempts` | Renames the table and the columns of the run log ([the spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/units.md#the-database-migration)). |
| 5, `queue_identity_kind` | Adds the column of the identity's kind to the sqlite queue table. A message that is queued with an identity when it is applied has no kind, and its job fails (`InvalidJobIdentity`). |
| 6, `attempt_constraints` | Renames the Postgres constraints of `attempts` (`agent_runs_pkey` and the others) to `attempts_...`. Nothing on sqlite. |

Take a copy of a database that matters before you connect to it with the new version, as you would for any upgrade. There is no way back: a database that was migrated cannot be opened by 0.1.

## What is new

None of this needs a change; it is here so that you know it exists.

- **[Who is asking](agents.md#who-is-asking-the-identity-of-a-run):** a typed identity for a run, for tools, in your process and on workers (`identity_type`, `identity=`).
- **[Jobs that are not declared in code](submitted-jobs.md#jobs-that-are-not-declared-in-code):** `jobs_lookup` on a worker.
- **[Reading a skill from somewhere else](skills.md#reading-a-skill-from-somewhere-else):** `Skill.aread`.
- **[Attachments in a chat](streaming.md#attachments):** images, documents, audio and video, with limits (`max_attachments`, `max_bytes`). A file for an agent or a job is [passed as a reference](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/files-for-an-agent-or-a-job.md), and not carried.
- **[Configuration](agents.md#configuration):** one list of `FTW_` variables. New: `FTW_MAX_BACKGROUND_JOBS` and `FTW_WORKER_CONCURRENCY` (an argument wins over them), `FTW_JOBS_LOOKUP_TIMEOUT` and `FTW_SKILL_READ_TIMEOUT` (15 s each; environment only).
- **[Recipes](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/README.md)** and a glossary that says which name to use.

## For coding agents

You are changing a project that uses featherweight 0.1 to the next release. Work in this order, and stop to ask the owner where the page says to.

1. **Find the uses.** From the project root, run these and read every hit (`rg` or `git grep -n`):

   ```
   rg -n "RunStore|store_get|store_set|background_agents|agent_run_id|invocation_id|AgentRunRow|AgentRunFailed|RunStatus|ReusedRun|RunStart|RunEnd|max_background=|list_runs|get_run|create_run|finish_run|mark_run_running|find_reusable_run|agent_runs|MAX_CONCURRENT_AGENT_RUNS|ftw\.run\.id|ftw\.invocation\.id|reused_from_agent_run_id|\binvocation_(started|finished)\b"
   ```

2. **Remove what is gone,** by hand: the run store ([the first section](#the-run-store-is-gone)). It has no replacement; ask the owner where the state should live if the code relied on it across agents.
3. **Rename mechanically,** with whole-word matches, **in this order** (a later row can match what an earlier row wrote):

   | # | Find | Replace with | Only in |
   |---|---|---|---|
   | 1 | `parent_agent_run_id` | `parent_attempt_id` | code and SQL |
   | 2 | `reused_from_agent_run_id` | `reused_from_attempt_id` | code and SQL |
   | 3 | `agent_run_id` | `attempt_id` | code, SQL and logs |
   | 4 | `CURRENT_AGENT_RUN_ID` | `CURRENT_ATTEMPT_ID` | code |
   | 5 | `invocation_id` | `run_id` | code and SQL |
   | 6 | `AgentRunRow` | `AttemptRow` | code |
   | 7 | `AgentRunFailed` | `RunFailed` | code |
   | 8 | `list_runs`, `get_run`, `create_run`, `finish_run`, `mark_run_running`, `find_reusable_run` | `list_attempts`, `get_attempt`, `create_attempt`, `finish_attempt`, `start_attempt`, `find_reusable_attempt` | calls on a database object, and a `Database` you implement |
   | 9 | `agent_runs` | `attempts` | SQL |
   | 10 | `background_agents` | `background_jobs` | code, docs and prompts |
   | 11 | `MAX_CONCURRENT_AGENT_RUNS` | `FTW_MAX_CONCURRENT_ATTEMPTS` | deployment files, `.env`, docs |
   | 11b | `RunStatus` | `Status` | code (the import and every use) |
   | 11c | `ReusedRun` | `RunReused` | code that implements an observer |
   | 11d | `max_background=` | `max_background_jobs=` | `AgentRunner(...)` calls |
   | 12 | `ftw.run.id` | `ftw.attempt.id` | span queries, dashboards, alerts: **before row 13** |
   | 13 | `ftw.invocation.id` | `ftw.run.id` | span queries, dashboards, alerts |
   | 14 | `ftw.reused_from_agent_run_id` | `ftw.reused_from_attempt_id` | span queries |
   | 15 | the log lines `run_created`, `run_running`, `run_finished` | `attempt_created`, `attempt_running`, `attempt_finished` | log queries and alerts: **before row 16** |
   | 16 | `invocation_started`, `invocation_finished` | `run_started`, `run_finished` | log queries and alerts |

4. **Ask the owner** about: a chat that looks up the row of a stream by the `run_id` of its events (`get_attempt(` or `get_run(` with a stream's id, which is now `None`: use the stream's `attempt_id`, or `list_attempts(run_id=...)`), a `Database` or a queue the project implements (change them as the two sections above say, then run the kits), dashboards, alerts and log queries that are not in the repository, and what [the stricter checks](#stricter-checks-identity-job-names-and-skills) and [the attachments table](#attachments-in-a-chat) list: one `job_id` given to runs for several identities, an identity model with an `exclude=True` field and no default, job names built from user input, a skill whose `aread` can return `None`, an `except ValueError` around `runner.stream()` (answer `AttachmentLimitError` with a 413), and a client that matches the text of an error message.
5. **Check.** Run the rename search from step 1 again: it must return nothing outside this page. Run the project's tests. If the project has a database that matters, test the upgrade on a copy. Then run `git diff --stat` and read it: every change should be one of the rows above.
6. **Do not** rename a variable that happens to be called `run_id` or `attempt_id` in the project's own code, and do not touch the OpenTelemetry span names.
