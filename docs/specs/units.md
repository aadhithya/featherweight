# Units of work

Status: implemented. It was [epic #215](https://github.com/aadhithya/featherweight/issues/215): this spec ([#216](https://github.com/aadhithya/featherweight/issues/216)),
the code ([#217](https://github.com/aadhithya/featherweight/issues/217)), the docs ([#218](https://github.com/aadhithya/featherweight/issues/218)), the migration document
([#219](https://github.com/aadhithya/featherweight/issues/219)) and the decisions ([#220](https://github.com/aadhithya/featherweight/issues/220), built with the code). All of them are done.

It owns the words for the units that featherweight does work in, and the names that those words give to ids, tables, columns, spans and log lines. The other specs use them and
link here: [the runner](runner.md) (attempts and retries), [jobs](jobs.md), [submitted jobs](submitted-jobs.md), [persistence](persistence.md) (the schema) and
[observers and OpenTelemetry](observability.md) (the attributes).

- [Goal](#goal)
- [The units](#the-units)
- [How they nest](#how-they-nest)
- [Cases](#cases)
- [Where each unit appears](#where-each-unit-appears)
- [The rename](#the-rename)
- [What does not change](#what-does-not-change)
- [The database migration](#the-database-migration)
- [Decisions](#decisions)
- [Decided by the owner](#decided-by-the-owner-2026-11-24)
- [Decided with them](#decided-with-them-for-the-same-reason-names-that-should-not-change-again)
- [Tests](#tests)
- [The work](#the-work)

## Goal

A word names one unit of work, in the code, the database, the spans, the logs and the docs, so that nobody has to guess. Today "run" is one call of an agent, one try of it, and in
some places a job; "invocation" is a second name for the first; the id called `agent_run_id` is the id of an **attempt**, as are `AgentResult.agent_run_id` and the span attribute
`ftw.run.id`; and "execution" sits next to `execute_job` and `execute_tool`. This spec gives each unit one word and one id, and renames what does not match.

## The units

| Unit | The word | Its id | What it is |
|---|---|---|---|
| A pipeline declared as a class or with `@job` | **job** | `job_id` | It has a name, a status, events and a result. It is what a client submits and a worker runs. |
| One start of a job by a process or a worker | **execution** | `job_execution` (a count: `executions`) | A job starts once, and again after a takeover or after a shutdown put it back on the queue. Not a try of an agent. |
| One call of an agent, with its retries | **run** | `run_id` | What `runner.run(agent, input)` is, and what returns an `AgentResult`. One streamed turn is a run. |
| One try within a run | **attempt** | `attempt_id` | A run makes up to `max_attempts` of them. Each is a row of the run log, and one `invoke_agent` span. |
| A run made directly by the job's own code (depth 1) | **step** | none | A role of a run, not a unit of its own: it is the run that resume reuses. |

- **One word, one unit.** A word is never used for another unit in the docs, and a name that holds it names that unit only: `run_id` is a run's, `attempt_id` an attempt's.
- **"Invocation" is retired.** It was a second name for a run. **"Agent run" is "run".** An attempt is never "a run", and a job is never "a run of a pipeline".
- **"Run" as a verb stays:** to run an agent, to run a job, `run_many`, `run_job`. As a noun, "a run" is one call of an agent, always.
- **The run log** is the record of jobs and attempts (a run has no row of its own: its attempts share a `run_id`). The name stays.

## How they nest

```
job ── one or more executions (a takeover, or a requeue after a shutdown, makes another)
         └── the job's code makes runs (the ones made directly are its steps)
               └── each run makes one or more attempts (a retry is another attempt, with the same run_id)
                     └── an attempt may launch sub-agents: each is a run of its own, one level deeper
                     └── an attempt may start background jobs: each is a job of its own
```

## Cases

- **A retry** is another attempt of the same run: a new `attempt_id`, the same `run_id`.
- **A streamed turn** is one run with one attempt, never retried. The events carry its `run_id` (AG-UI's `run_id`), and its row has an `attempt_id` of its own.
- **A sub-agent** is launched by an attempt of its parent: its run is one level deeper (`depth`), and its `parent_attempt_id` is the attempt that launched it.
- **A reused run** (a resume answers a step from a kept result) is a run with one attempt row, with zero tokens and `reused_from_attempt_id` naming the attempt whose result it took.
- **A background job** is a job of its own, started by an attempt of the agent that listed it: its `parent_attempt_id` is that attempt. Its runs are those of any job.
- **A takeover** is another execution of the same job. The runs of the earlier executions are looked up by it (`job_execution` below the current one), as before.
- **A resume** is a new job that points to an old one (`resumed_from`) and reuses its steps, for the same agent, input and identity.

## Where each unit appears

| | job | execution | run | attempt |
|---|---|---|---|---|
| **API** | `Job`, `run_job`, `JobClient.submit`, `JobContext` | `JobRow.executions` | `runner.run`, `run_many`, `stream`, `AgentResult`, `StreamingRun` | `AgentResult.attempts`, `RunContext.attempt_id` |
| **Ids** | `job_id` | `job_execution` | `run_id` | `attempt_id` |
| **Database** | `jobs` | `jobs.executions`, `attempts.job_execution` | `attempts.run_id` | the table `attempts` |
| **Observers** | `JobStart`, `JobEnd` | `JobStart.execution` | `run_id` on the attempt records | `AttemptStart`, `AttemptEnd` |
| **Spans** | `invoke_workflow` | `ftw.job.execution` | `ftw.run.id` | `invoke_agent`, `ftw.attempt.id`, `ftw.attempt` |
| **Logs** | `job_started`... | `worker_takeover` | `run_started`, `run_finished`, `run_reused` | `attempt_started`, `attempt_created`, `attempt_finished` |
| **Settings** | `FTW_MAX_BACKGROUND_JOBS` | | | `FTW_MAX_CONCURRENT_ATTEMPTS` |

## The rename

From a complete audit of the repository: every identifier in `src`, `tests`, `examples` and the book's code that contains "run", "invocation" or "agent_run"; every table, column and index;
every log line, span attribute and observer field; every setting; and every page of the docs. What does not appear in a table here is not renamed.

**The Python API**

| Today | After | Notes |
|---|---|---|
| `AgentResult.agent_run_id` | `AgentResult.attempt_id` | The id of the last attempt. `AgentResult` also gets `run_id`. |
| `RunContext.agent_run_id` | `RunContext.attempt_id` | The attempt that is running. `RunContext` also gets `run_id`. The class keeps its name. |
| `CURRENT_AGENT_RUN_ID` | `CURRENT_ATTEMPT_ID` | The context variable (and the string of its name). |
| `AgentRunRow` | `AttemptRow` | In `featherweight.db`, and exported from `featherweight`. |
| `RunStart`, `RunEnd` | `AttemptRecord`, `AttemptFinish` | In `featherweight.db`, and exported from `featherweight`: what a `Database` is given to create and to finish an attempt. Decided. |
| `Database.create_run`, `mark_run_running`, `finish_run`, `get_run`, `list_runs` | `create_attempt`, `start_attempt`, `finish_attempt`, `get_attempt`, `list_attempts` | A `Database` you wrote renames them too. |
| `Database.list_runs(invocation_id=)` | `list_attempts(run_id=)` | |
| `Database.find_reusable_run` | `find_reusable_attempt` | It returns the attempt row of a step that finished. |
| `JobRow.parent_agent_run_id`, `AttemptRow.parent_agent_run_id` | `parent_attempt_id` | The attempt that launched the agent, or started the job. |
| `AttemptRow.reused_from_agent_run_id` | `reused_from_attempt_id` | |
| `AttemptRow.invocation_id` | `run_id` | |
| `AgentRunFailed` (in `featherweight.jobs`) | `RunFailed` | Raised by a job made from an agent whose run failed. |
| `RunStatus` | `Status` | The status of a job, an attempt and a result. Decided. |
| `ReusedRun` (observer record) | `RunReused` | Decided. |
| `AgentRunner(max_background=)` | `max_background_jobs` | Decided. |
| `StreamingRun.run_id` | `StreamingRun.run_id` (unchanged name) | Now really a run id; `runner.cancel(run_id)` takes it. |

**The database** ([migration 4](#the-database-migration))

| Today | After |
|---|---|
| table `agent_runs` | `attempts` |
| `agent_run_id` | `attempt_id` |
| `invocation_id` | `run_id` |
| `parent_agent_run_id` (in `agent_runs` and in `jobs`) | `parent_attempt_id` |
| `reused_from_agent_run_id` | `reused_from_attempt_id` |
| indexes `idx_agent_runs_job`, `_invocation`, `_parent`, `_reuse`, `_seq` | `idx_attempts_job`, `_run`, `_parent`, `_reuse`, `_seq` |
| Postgres constraints `agent_runs_pkey`, `_job_id_fkey`, `_attempt_check`, `_depth_check`, `_input_tokens_check`, `_latency_ms_check`, `_output_tokens_check`, `_status_check` | `attempts_pkey`, `attempts_job_id_fkey`, ... (migration 6: Postgres does not rename them with the table) |

**Observers' records** (`featherweight.observers`): in `AttemptStart`, `AttemptEnd`, `ReusedRun` and the tool and model records, `agent_run_id` becomes `attempt_id` and
`invocation_id` becomes `run_id`; in `ReusedRun`, `reused_from_agent_run_id` becomes `reused_from_attempt_id`; in `JobSubmitted`, `parent_agent_run_id` becomes `parent_attempt_id`.

**OpenTelemetry attributes**

| Today | After | Notes |
|---|---|---|
| `ftw.invocation.id` | `ftw.run.id` | **The meaning of `ftw.run.id` changes:** today it is an attempt's id. A dashboard or an alert that reads it must change. |
| `ftw.run.id` (an attempt's id) | `ftw.attempt.id` | |
| `ftw.reused_from_agent_run_id` | `ftw.reused_from_attempt_id` | |

**Log lines** (`key=value` lines of the `featherweight` logger)

| Today | After | Where |
|---|---|---|
| `invocation_started`, `invocation_finished` | `run_started`, `run_finished` | The runner, once per run. |
| `run_created`, `run_running`, `run_finished` | `attempt_created`, `attempt_running`, `attempt_finished` | The databases, once per attempt row. |
| the key `agent_run_id=` | `attempt_id=` | In every line that carries it. |
| the key `invocation_id=` | `run_id=` | |
| `run_reused attempt_id=... reused_from=...`, `run_reuse_rejected reused_from=...`, `result_missing reused_from=...` | `run_reused run_id=... attempt_id=... reused_from_attempt_id=...`, and the other two with `run_id=` and `reused_from_attempt_id=` | The lines of a resume. A reused run is a run with one attempt row: its line names both ids, and the key says that it holds an attempt id. |

Unchanged: `attempt_started`, `stream_started`, `stream_ended`, `fan_out_started`, `fan_out_finished`.

**Settings:** the environment variable `MAX_CONCURRENT_AGENT_RUNS` becomes `MAX_CONCURRENT_ATTEMPTS` (and then `FTW_MAX_CONCURRENT_ATTEMPTS`, [configuration](configuration.md)): it limits the attempts (the model calls) that run at once, not the runs. The argument `max_concurrent` keeps its name.

**The examples:** the doclearn example saves what a run of its command produces in a "run folder" (`runs/<time>_<category>/`, `make_run_dir`, `setup_run_logging`, 24 mentions in the docs, the Makefile and `.gitignore`). That is a run of a command line, not of an agent, so it becomes the **output folder** (`out/<time>_<category>/`, `make_output_dir`). `examples/resume` prints attempts (`print_runs` becomes `print_attempts`).

**The docs:** "invocation" and "agent run" become "run" (about 13 and 40 uses); a row of the run log is an attempt; "a job is one run of a pipeline" is reworded; the glossary says which words are retired.

## What does not change

- The OpenTelemetry **span names** (`invoke_workflow`, `invoke_agent`, `chat`, `execute_tool`): they follow the GenAI conventions, which are not ours to rename.
- AG-UI's own words and classes (`RUN_STARTED`, `RunStartedEvent`, `RunAgentInput`): they are the protocol's, and they agree with ours: a run is one call of an agent.
- `execute_job` (it runs one execution of a job), `execute_tool`, `AgentRunner`, `StreamingRun`, `run`, `run_many`, `run_job`, `JobContext.run`.
- `max_attempts`, `attempts`, `attempt` (the number), `executions`, `job_execution`, `depth`, `step`.

## The database migration

Migration 4 renames in place and keeps every row, on sqlite and on Postgres. A database made by 0.1 (and migrated to 2 and 3) upgrades when it is connected, as before.

```sql
-- sqlite
ALTER TABLE agent_runs RENAME TO attempts;
ALTER TABLE attempts RENAME COLUMN agent_run_id TO attempt_id;
ALTER TABLE attempts RENAME COLUMN invocation_id TO run_id;
ALTER TABLE attempts RENAME COLUMN parent_agent_run_id TO parent_attempt_id;
ALTER TABLE attempts RENAME COLUMN reused_from_agent_run_id TO reused_from_attempt_id;
ALTER TABLE jobs RENAME COLUMN parent_agent_run_id TO parent_attempt_id;
DROP INDEX idx_agent_runs_job; CREATE INDEX idx_attempts_job ON attempts(job_id);   -- and the four others, under their new names
```

Postgres does the same with `ALTER INDEX ... RENAME TO` for the indexes. A test migrates a database that has rows in it (an attempt with a parent, a reused attempt, a job started by an
attempt) and checks that they come back under the new names.

## Decisions

- **A run is one call of an agent.** It is what the API calls (`run`), what returns a result, and what AG-UI calls a run. "Invocation" meant the same, so it goes.
- **The rows are attempts.** A row of the run log is one try, which is what `agent_runs` always held; the table and the ids now say so. The name "run log" stays: it records runs, through their attempts, and jobs.
- **A step is a role.** It is a run at depth 1 of a job. Resume reuses it. It is not a unit with an id of its own.
- **A streamed turn has a `run_id` and an `attempt_id`.** They are two ids, because they name two units, even if a streamed run has one attempt.
- **`AgentResult` gets both ids,** so that a caller can find the run (`run_id`) and its last attempt (`attempt_id`) in the log.
- **`ftw.run.id` changes meaning,** and that is said in the migration document and the release notes. The alternative, a new name for a run id, would leave the wrong word on a unit that is called a run.
- **One setting is renamed too** (`MAX_CONCURRENT_AGENT_RUNS`), because it counts attempts, and a setting that is wrong about its unit is the same mistake as an id.
- **No aliases** (0.x): the old names are removed, and [the migration document](../migration.md) lists each with what to write instead.

## Decided by the owner (2026-11-24)

- **`RunStatus` is `Status`.** It is the status of a job, an attempt and a result, none of them a run in these words. It is exported as `Status` (and the module-level uses follow), with the same members:
  `INIT`, `RUNNING`, `SUCCESS`, `FAILED` and `CANCELLED`.
- **The database record classes are `AttemptRecord` and `AttemptFinish`** (they were `RunStart` and `RunEnd`), because `featherweight.observers` has `AttemptStart` and `AttemptEnd`, which are something else
  (what an observer hears). A person who writes a `Database` does not meet two classes of one name.
- **The verb `run_job` stays,** with the rule above that the verb "run" starts anything and the noun "a run" is one call of an agent.
- **The doclearn example's run folder is the output folder.**

## Decided with them, for the same reason (names that should not change again)

A scan of the public names, after the renames above, found three that are not parallel to their neighbours. The owner accepted the renames, and they are in the rename tables.

| Today | After | Why |
|---|---|---|
| `ReusedRun` (observer record) | `RunReused` | The other records of an event are `<Unit><Event>`: `JobSubmitted`, `JobStart`, `AttemptEnd`. |
| `Database.mark_run_running` | `Database.start_attempt` | A job's `start_job` and an attempt's start are the same step, and the lifecycle reads `create_attempt`, `start_attempt`, `finish_attempt`. |
| `AgentRunner(max_background=)` | `max_background_jobs` (and the setting `FTW_MAX_BACKGROUND_JOBS`: [configuration](configuration.md)) | The other limits name what they count: `max_launches`, `max_depth`, `max_concurrent`; and the field is `background_jobs`. |

**Considered, and left as they are:** `AgentResult` (the result of a run) and `JobOutcome` (the outcome of a job) use two words for two similar things, and `outcome.result` holds what `result.output` holds. They are the two types
that users meet most, the difference between a result and an outcome is a real one (a job can be `CANCELLED`, an agent's result is a value), and renaming them would change every example for a gain that is small.

## Tests

- **The database migration:** a database made by 0.1 with rows migrates to 4 and keeps them under the new names, on sqlite and on Postgres; `testing.check_database` uses the new names.
- **No old name remains:** a test greps `src`, `examples` and the book's code for every name in the tables above and fails on a hit (the migration document is the one place that lists them).
- **The ids:** an `AgentResult` has a `run_id` that is the `run_id` of its attempts' rows and an `attempt_id` that is its last attempt's; a retry has the same `run_id` and another `attempt_id`;
  a streamed run's events carry its `run_id`; the spans carry `ftw.run.id` and `ftw.attempt.id`.
- **The words:** the terms test rules out "invocation" and "agent run" in prose.

## The work

| Issue | |
|---|---|
| [#215](https://github.com/aadhithya/featherweight/issues/215) | The epic |
| [#216](https://github.com/aadhithya/featherweight/issues/216) | This spec |
| [#217](https://github.com/aadhithya/featherweight/issues/217) | The code: the database, the runner, the observers, the events, the logs and the spans (migration 4) |
| [#218](https://github.com/aadhithya/featherweight/issues/218) | The docs |
| [#219](https://github.com/aadhithya/featherweight/issues/219) | The migration document, and its reference in the skill |
| [#220](https://github.com/aadhithya/featherweight/issues/220) | The decisions (made 2026-11-24): `Status`, the record class names, `run_job`; built with the code |
