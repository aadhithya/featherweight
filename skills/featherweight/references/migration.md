<!-- Copied from docs/migration.md by scripts/build_skill.py. Edit the original, not this file. -->

# Migrating from 0.1

Everything that breaks between 0.1 and the next release, what to search for in your project, what to write instead, and how to check. It is for people, and for the coding agents that work on projects that use featherweight: the last section is written for them, and the skill carries the same page.

The rename of the units is [specified](https://github.com/aadhithya/featherweight/blob/main/docs/specs/units.md), and a test checks every name on this page against the code.

- [What breaks, in the order you are likely to meet it](#what-breaks)
- [The run store is gone](#the-run-store-is-gone)
- [`background_agents` is `background_jobs`](#background_agents-is-background_jobs)
- [Runs, attempts and executions](#runs-attempts-and-executions)
- [A `Database` you wrote](#a-database-you-wrote)
- [A queue you wrote](#a-queue-you-wrote)
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
| 6 | [Settings, logs and spans](#settings-logs-and-spans) | set `MAX_CONCURRENT_AGENT_RUNS`, read the logs, or have dashboards or alerts on the spans |

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

**SQL**, if you query the run log yourself: the table `agent_runs` is `attempts`; the columns `agent_run_id`, `invocation_id`, `parent_agent_run_id` (also in `jobs`) and `reused_from_agent_run_id` are `attempt_id`, `run_id`, `parent_attempt_id` and `reused_from_attempt_id`.
The database is migrated for you: [see below](#your-database-upgrades-by-itself).

Check: the old names are gone from your code (`git grep -n "agent_run_id\|invocation_id\|list_runs\|AgentRunRow"` is empty), and your tests pass.

## A `Database` you wrote

- Your methods are renamed as in the table above (`create_attempt`, `start_attempt`, `finish_attempt`, `get_attempt`, `list_attempts`, `find_reusable_attempt`), and the records and rows too.
- `find_reusable_attempt` takes `identity_hash` ([who is asking](agents.md#who-is-asking-the-identity-of-a-run)): a step is reused only when the old attempt's `identity_hash` equals the new one, and `None` matches only `None`. The attempt record has an `identity_hash` field.
- Your schema has the table `attempts` with the renamed columns and a nullable `identity_hash` column.
- Run `testing.check_database` against it, which checks all of this.

## A queue you wrote

A message has a new field, `identity` (a JSON mapping or `None`): carry it unchanged. Your queue stores it with the other fields, and returns it. `testing.check_queue` checks that it does, and a queue that drops it fails.

## Settings, logs and spans

| Before | After | Notes |
|---|---|---|
| the environment variable `MAX_CONCURRENT_AGENT_RUNS` | `MAX_CONCURRENT_ATTEMPTS` | It limits the attempts (the model calls) that run at once. The argument `max_concurrent` is unchanged. |
| the span attribute `ftw.invocation.id` | `ftw.run.id` | |
| the span attribute `ftw.run.id` | `ftw.attempt.id` | **`ftw.run.id` now means something else** (a run, not an attempt): change dashboards and alerts that read it before you upgrade the attribute they read. |
| the span attribute `ftw.reused_from_agent_run_id` | `ftw.reused_from_attempt_id` | |
| the log lines `invocation_started`, `invocation_finished` | `run_started`, `run_finished` | One per run. |
| the log lines `run_created`, `run_running`, `run_finished` (the run log's rows) | `attempt_created`, `attempt_running`, `attempt_finished` | One per attempt. Mind the order if you rewrite them by script: `run_finished` is both a before and an after. |
| the keys `agent_run_id=` and `invocation_id=` in the log lines | `attempt_id=` and `run_id=` | |

The span **names** do not change (`invoke_workflow`, `invoke_agent`, `chat`, `execute_tool`).

## Your database upgrades by itself

`SqliteDatabase` and `PostgresDatabase` apply the migrations they have not applied when they connect, in a transaction each, and keep every row:

| Migration | What it does |
|---|---|
| 2, `identity_hash` | Adds `agent_runs.identity_hash`. Runs from before it have none. |
| 3, `queue_identity` | Adds the column of the identity to the sqlite queue table. |
| 4, `attempts` | Renames the table and the columns of the run log ([the spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/units.md#the-database-migration)). |

Take a copy of a database that matters before you connect to it with the new version, as you would for any upgrade. There is no way back: a database that was migrated cannot be opened by 0.1.

## What is new

None of this needs a change; it is here so that you know it exists.

- **[Who is asking](agents.md#who-is-asking-the-identity-of-a-run):** a typed identity for a run, for tools, in your process and on workers (`identity_type`, `identity=`).
- **[Jobs that are not declared in code](submitted-jobs.md#jobs-that-are-not-declared-in-code):** `jobs_lookup` on a worker.
- **[Reading a skill from somewhere else](skills.md#reading-a-skill-from-somewhere-else):** `Skill.aread`.
- **[Attachments in a chat](streaming.md):** images, documents, audio and video, with limits (`max_attachments`, `max_bytes`).
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
   | 11 | `MAX_CONCURRENT_AGENT_RUNS` | `MAX_CONCURRENT_ATTEMPTS` | deployment files, `.env`, docs |
   | 11b | `RunStatus` | `Status` | code (the import and every use) |
   | 11c | `ReusedRun` | `RunReused` | code that implements an observer |
   | 11d | `max_background=` | `max_background_jobs=` | `AgentRunner(...)` calls |
   | 12 | `ftw.run.id` | `ftw.attempt.id` | span queries, dashboards, alerts: **before row 13** |
   | 13 | `ftw.invocation.id` | `ftw.run.id` | span queries, dashboards, alerts |
   | 14 | `ftw.reused_from_agent_run_id` | `ftw.reused_from_attempt_id` | span queries |
   | 15 | the log lines `run_created`, `run_running`, `run_finished` | `attempt_created`, `attempt_running`, `attempt_finished` | log queries and alerts: **before row 16** |
   | 16 | `invocation_started`, `invocation_finished` | `run_started`, `run_finished` | log queries and alerts |

4. **Ask the owner** about: a `Database` or a queue the project implements (change them as the two sections above say, then run the kits), and dashboards, alerts and log queries that are not in the repository.
5. **Check.** Run the rename search from step 1 again: it must return nothing outside this page. Run the project's tests. If the project has a database that matters, test the upgrade on a copy. Then run `git diff --stat` and read it: every change should be one of the rows above.
6. **Do not** rename a variable that happens to be called `run_id` or `attempt_id` in the project's own code, and do not touch the OpenTelemetry span names.
