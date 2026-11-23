# Spec: identity

Status: designed, not built. It is [epic #190](https://github.com/aadhithya/featherweight/issues/190), with an issue for each part: the runner and the tools
([#195](https://github.com/aadhithya/featherweight/issues/195)), the queue and the worker ([#196](https://github.com/aadhithya/featherweight/issues/196)) and the
identity hash ([#197](https://github.com/aadhithya/featherweight/issues/197)). When a part is built, this page says so, and the guide shows how to use it.

It builds on [the runner](runner.md), [tools and the registry](tools-and-store.md), [submitted jobs](submitted-jobs.md) and [persistence](persistence.md),
and adds a few lines to each. What it owns is the identity itself.

- [Goal](#goal)
- [The identity](#the-identity)
- [Declaring and passing it](#declaring-and-passing-it)
- [How tools read it](#how-tools-read-it)
- [Who inherits it](#who-inherits-it)
- [Across the queue](#across-the-queue)
- [Resuming a job](#resuming-a-job)
- [What featherweight never does with it](#what-featherweight-never-does-with-it)
- [The developer's responsibilities](#the-developers-responsibilities)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)
- [The work](#the-work)

## Goal

A tool often has to act for someone: read this user's files, send this user's mail, count against this tenant's quota. **The model must not choose who**, and
the answer cannot come from the model's input. An identity is what the application tells a run about who it is for. It is a typed value that reaches the tools
and nothing else, and it is the same value whether the run happens in your process or on a worker ("one concept, two places": a tool that works in a process
must not silently get nothing on a worker).

featherweight **carries** the identity. It does not authenticate anyone, decide what they may do, get a credential for them or keep them: those are the
application's ([the developer's responsibilities](#the-developers-responsibilities)). This is the principle of the framework applied once more: a typed seam,
checked at the boundary and closed when misused, and no solution built behind it ([the philosophy](../book/00-start-here/03-philosophy.md)).

## The identity

An identity is a Pydantic model that the application defines:

```python
class Teacher(BaseModel):
    user_id: str
    tenant: str
    roles: list[str] = []
    language: str = "en"
```

- It holds what the tools need to know about who the run is for: ids, a tenant, roles, a language. Whatever it holds is the application's: featherweight does
  not read its fields.
- **Small.** It travels in the queue message of a submitted job, so it holds ids, not names or addresses.
- **No secrets.** A token in it would sit in the queue until the message is acknowledged, and a queued job can outlive a token. A tool gets a credential from the
  identity with the application's own code ([the responsibilities](#the-developers-responsibilities)). A field marked `exclude=True` is the one exception: it is
  not serialised, so it is dropped when a job is submitted, and it is not part of the [hash](#resuming-a-job). It is there for a value that exists only in your process.
- **It is a model, not a field of an agent.** An agent is a static definition and is not changed by who runs it: the same agent class runs for every user, and the
  identity is not in `agent_hash`.

## Declaring and passing it

The application declares the type once, where the framework needs it to check and to rebuild the identity:

```python
runner = AgentRunner(db, identity_type=Teacher)
worker = JobWorker(db, queue, runner, jobs=[...], identity_type=Teacher)
client = JobClient(db, queue, results, identity_type=Teacher)  # optional

result = await runner.run(Summarise, summarise_input, identity=teacher)
```

**`identity=` is an argument of `run`, `run_many`, `stream`, `run_job` and `JobClient.submit`.** The declared type says what it must be:

| Declared as | A run without `identity=` | A run with `identity=` |
|---|---|---|
| nothing | fine: no identity | `TypeError`: the framework could not rebuild it on a worker |
| `Teacher` | `TypeError` | checked: it must be a `Teacher` |
| `Teacher \| None` | fine: no identity | checked: it must be a `Teacher` |

- **A missing identity is a programming error and raises**, like an input of the wrong type, and before anything runs. The case it prevents: one code path of
  an application forgets `identity=`, and a tool runs as nobody. Declaring `Teacher | None` is how an application says that some runs (a nightly job) have none.
- `run_many` gives every input the same identity. `run_job` and `submit` give it to the job: every agent the job runs with `ctx.run` has it.
- **The runner and the worker declare the same type.** The worker rebuilds the identity from the message with its type. **The client's declaration is
  optional:** with it, `submit` checks the identity before it creates a job (`TypeError`); without it, `submit` accepts any Pydantic model and serialises it, and
  the worker is where the declared type is the authority.

## How tools read it

`RunContext` becomes generic and gets a field:

```python
@dataclass
class RunContext(Generic[IdentityT]):
    job_id: str | None
    agent_run_id: str
    identity: IdentityT | None
```

A tool declares `runtime: ToolRuntime[RunContext[Teacher]]` and reads `runtime.context.identity`. LangChain fills the parameter in and hides it from the model, so
the model never passes it and cannot see it ([tools and the registry](tools-and-store.md#the-run-context)). A tool that does not declare `runtime` cannot see it.

- **The shared (MCP) tools** cannot take a `RunContext`, because one tool object serves many attempts. The runner sets a context variable around each attempt,
  as it does for the attempt's id, and code that cannot take `runtime` reads it: `CURRENT_IDENTITY`.
- **Pipeline code** reads the identity of its job with `ctx.identity` on the `JobContext`, for a job that has to behave differently for a tenant.
- The identity is the **same object** that was passed, in a process. On a worker it is a new object, rebuilt from the message, and equal to it.

## Who inherits it

- **A launched agent** (`launch_agent`) runs with the identity of its parent, and so does an agent started in the background in the same process.
- **A background job on a worker** (a runner with `background=JobClient`) is submitted with the identity of its parent.
- **The model cannot set, change or drop it.** `launch_agent` and `start_background` take no identity argument.

**Why identity is inherited and capabilities are not.** A launched agent does not get its parent's tools, skills or sub-agents: each agent has the allow-lists its own
definition gives it, and that is least privilege. An identity is not a capability. It is *who the whole run acts for*, and a child that ran as nobody, or as
someone else, would make its tools act for the wrong person or for no one. So a child can never have more, or less, than its parent's identity: it is the
same one. What the child **can do** with it stays limited by its own allow-lists.

## Across the queue

A submitted job is the one place where a run leaves the process, so it is the one place where the identity is serialised, and the only place where it could be read or changed
by anything but the application.

- `JobMessage` gets a field `identity: dict[str, Any] | None`: the identity as JSON, from `model_dump(mode="json")`, without the fields marked `exclude=True`.
- **The worker rebuilds it** with its declared type and gives it to the run, so a tool sees on a worker what it sees in a process.
- **It fails closed.** A message whose identity does not validate against the worker's type, and a message without one when the worker's type does not allow `None`,
  make the job `FAILED` with `InvalidJobIdentity` (the way an input that does not validate does): the job is not run, and the worker does not retry it.
- **Every queue carries it,** and `testing.check_queue` checks that a queue returns it unchanged, so that a queue that drops the field fails the kit. `SqliteQueue`
  stores it in the `job_queue` table (a migration) until the message is acknowledged, on the same terms as the input.
- **The queue is trusted.** featherweight does not sign or encrypt a message: a deployment secures its queue (access control, TLS, encryption at rest) as it does for the
  input. An application that wants a signature or encryption of its own writes or wraps a queue: `JobQueue` is the seam, and a wrapper that signs the message on
  `enqueue` and checks it on `receive` is a [recipe](../recipes/README.md), run through `check_queue` like any queue.

## Resuming a job

[Resuming](runner.md#resuming) reuses a step of an old job when the agent hash and the input hash match. That assumes a step depends only on its agent and its input. A tool that
reads the identity makes it depend on who ran it, and if the identity has changed, so may the permissions, so the old result cannot be relied on. So the identity takes part, as a hash:

- Each run records **`identity_hash`**: the SHA-256 of the identity as canonical JSON (keys sorted, no spaces: the form `input_hash` uses), without the fields
  marked `exclude=True`, so that it is the same in a process and on a worker. **`NULL` when the run had no identity.** The framework stores no identity.
- A step is reused only when the old run's `identity_hash` **equals** the new run's. `NULL` equals only `NULL`: a step that ran with no identity is not reused by a run
  with one, and the other way round.
- This is a rule about **which steps are the same step.** Whether a person may resume a job at all, or see its result, is the application's check ([the
  responsibilities](#the-developers-responsibilities)).
- A support person who resumes a user's job under their own identity reruns every step. An application that wants the job to keep acting as the user passes the user's
  identity, once it has decided that it may. Identity should therefore stay small and stable: a roles list that changes between two runs stops the reuse.

## What featherweight never does with it

- **It is never shown to the model:** not in the prompt, not in a tool's arguments, not in a tool's result that the runner writes, and not in the events of a stream.
- **It is never in `agent_hash`,** `system_prompt_hash` or `tools_hash`: only its hash is recorded, on the run.
- **It is never stored.** No table holds it. The queue message holds it until it is acknowledged, as it holds the input.
- **It is never logged,** and **never in a span or an observer record** (observers hear metadata): `OTelObserver` does not put it on a span. An application that wants a tenant
  on its traces adds it itself, in its own observer.

## The developer's responsibilities

These stay the application's, on purpose, and the guides, the book and the recipes say so:

- **A tool checks authorization itself.** Carrying an identity does not make a tool safe: a tool that ignores it acts for everyone. A tool should check what the identity may do
  before it acts, and refuse in text, as the ready-made tools do.
- **An identity is a snapshot taken when the job was submitted.** A queued job can run minutes or hours later: roles may have changed, a user may have left, and a token
  will have expired. For a sensitive action, a tool re-checks with the application by user id, and does not trust a list of roles in the identity.
- **A tool gets credentials from the identity with its own code,** at each call (a long job outlives a token). The usual ways: a stored refresh token in a vault keyed by
  the user id, a service credential plus the user id (the service decides), a token exchange or impersonation, or a grant made at submit time and looked up by the job id.
- **Check who owns a job before `resume_from`.** The identity hash makes a resume reuse only steps of the same identity, and does not decide who may resume a job or read
  its result: that is a check the application makes with its own records of who started which job.
- **Keep it small,** and keep secrets out of it.
- **Do not let users declare stdio MCP servers.** A `StdioMCPServer` runs a command on your server. An application that builds agents from what users write allows only
  `HttpMCPServer`, or servers from a list it controls.
- **Names are the application's.** A job's name is a plain string it chooses, unique in what its [`jobs_lookup`](submitted-jobs.md#jobs-that-are-not-declared-in-code)
  searches, and stable: a rename breaks the jobs queued under the old name. The pattern is `^[A-Za-z][A-Za-z0-9_.-]*$`: a letter first, then letters, digits, `_`, `.` and `-`.

## Decisions

- **An identity is an argument of the run, not a field of the agent,** so the same agent runs for every user, and the hash of an agent says what it is, not who ran it.
- **Typed, declared, and required when declared:** the type says what a run must pass, and a union with `None` is how an application allows none. A missing identity raises,
  instead of letting a tool run as nobody.
- **It crosses the queue as a field of the message, rebuilt with the declared type,** and a message that does not fit fails the job. It is the one place where a run leaves the process.
- **Inherited by what a run launches, and the model cannot touch it.** It is the principal of the run, not a capability.
- **Identity, not credentials.** The framework has no credential provider and no protocol for one: where tokens live and how they are exchanged is the application's.
- **A hash on the run, so that resume reuses only the same step.** The framework keeps a hash, as it keeps `input_hash`, and never the identity.
- **No codec, no signing and no encryption in the framework.** The queue is the seam; a deployment secures it, and a wrapper queue is a recipe.
- **Plain strings for names, no key.** How an application names and keys what it defines is its own; the framework runs a job by the name it is given.

## Not supported

- **Authenticating a user,** deciding what they may do, and getting a credential for them.
- **An identity per tool call,** or a tool that changes it: a run has one.
- **Per-identity MCP servers** (a connection and headers for each identity). Deferred: the connection of a server is shared by every run of a runner today, and a server that
  acts for a user needs its own connection and a token issued for that server. It is its own piece of work.
- **Signing or encrypting a message,** and an identity that is a secret.
- **Storing identities,** a tenant column, and attributing runs to a user in the run log: the application keeps its own record of who started which job (see [tenants](persistence.md#tenants)).
- **A `key` on agents and jobs,** and an agent builder: how an application stores and names what its users define is its own.

## Tests

No live model call. Fake models, `MemoryQueue`.

- **Declaring and passing:** a missing, a wrong and an extra identity, for each of the three declarations; `Teacher | None`; each entry point (`run`, `run_many`,
  `stream`, `run_job`, `submit`).
- **Tools:** a tool of the parent, a tool of a launched agent and a sync tool (which runs in a thread) see the identity; the shared tools read `CURRENT_IDENTITY`; two runs
  with two identities on one runner at once do not mix; `ctx.identity` in a job.
- **Never:** the hash of an agent does not change with it; it is not in the prompt, the log, the run log or a span.
- **Inheritance:** a launched agent and an in-process background agent have it; a background job submitted to a worker carries it.
- **The queue:** `check_queue` against `SqliteQueue` and `MemoryQueue` (a queue that drops the field fails it); a job submitted with an identity whose tool reads it on a worker; a
  message without an identity for a worker that requires one, and a message that does not validate, fail the job and are not run.
- **Resume:** the same identity reuses, another one reruns, `NULL` matches only `NULL`; the hash is the same in a process and on a worker, and a field marked `exclude=True` is not in it; the `Database`
  kit on sqlite and Postgres.

## The work

| Issue | |
|---|---|
| [#190](https://github.com/aadhithya/featherweight/issues/190) | The epic |
| [#194](https://github.com/aadhithya/featherweight/issues/194) | This spec |
| [#195](https://github.com/aadhithya/featherweight/issues/195) | Identity in the runner and in tools |
| [#196](https://github.com/aadhithya/featherweight/issues/196) | Identity across the queue and the worker |
| [#197](https://github.com/aadhithya/featherweight/issues/197) | The identity hash: reuse on resume |
| [#198](https://github.com/aadhithya/featherweight/issues/198) | `Skill.aread` |
| [#199](https://github.com/aadhithya/featherweight/issues/199) | `jobs_lookup` |
| [#200](https://github.com/aadhithya/featherweight/issues/200) | The developer's responsibilities, and recipes for platform builders |
