# Identity

Status: implemented, in [epic #190](https://github.com/aadhithya/featherweight/issues/190): the runner and the tools ([#195](https://github.com/aadhithya/featherweight/issues/195): `identity_type`, `identity=`, `RunContext.identity`, `CURRENT_IDENTITY`, `JobContext.identity` and the inheritance), the identity hash ([#197](https://github.com/aadhithya/featherweight/issues/197): the `identity_hash` column, `fingerprint.identity_hash` and the reuse check) and the queue and the worker ([#196](https://github.com/aadhithya/featherweight/issues/196): the `identity` field of the message, `JobClient(identity_type=)` and `submit(identity=)`, `JobWorker(identity_type=)`, `InvalidJobIdentity`, the migration of the sqlite queue table, and the kit's check). The guides are [who is asking](../agents.md#who-is-asking-the-identity-of-a-run) and [the identity across the queue](../submitted-jobs.md#who-the-job-is-for-the-identity).

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
and nothing else, and it is the same value, apart from the fields marked `exclude=True`, whether the run happens in your process or on a worker ("one concept, two places": a tool that works in a process
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
  not serialised, so it is dropped when a job is submitted, and it is not part of the [hash](#resuming-a-job). It is there for a value that exists only in your process (a token that
  the tool would otherwise ask the vault for). **It needs a default** (`jwt: str | None = Field(None, exclude=True)`): the worker rebuilds the identity without it, and a model whose excluded field
  has none would fail every job on the worker, late, after `submit` had made and queued it. So `identity_type` is refused when the runner, the worker or the client is built if a model has an excluded
  field with no default. The pattern for a tool: in a process it may find the value; on a worker it is the default, and the tool falls back to the vault by user id.
- **One job, one identity.** A job is for one identity: every run that carries its `job_id` has the same identity, or none. To run something for another identity, use another job (a resume is a new job, so a
  support person can resume a user's job under their own identity). `run_job` and `submit` give the whole job one identity by construction; only a developer who passes the same `job_id` to `run`,
  `run_many` or `stream` with different identities can break it, and the runner [refuses it](#declaring-and-passing-it).
- **Read-only, by recommendation.** An identity is a value the application hands over: do not change it after a run has it. The hash is computed from the object at each run, so a tool that changes it
  changes what later runs record. featherweight does not enforce this (a frozen model is the application's choice).

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
| `Teacher` or `None` (a union) | fine: no identity | checked: it must be a `Teacher` |
| `Teacher` or `Student` (a union of models) | `TypeError` | checked: it must be one of them |
| `Teacher`, `Student` or `None` | fine: no identity | checked: it must be one of them |

- **`identity_type` is a Pydantic model class, or a union of model classes, with or without `None`.** Anything else, and a union that has two classes with the same `__name__` (two `Teacher` classes in two
  modules), raises `TypeError` when the runner, the worker or the client is built, not at the first run. A union is for one agent or job that serves different kinds of people, a teacher or a student: the
  application says which kinds, and the identity says which one this run is for ([the kind](#across-the-queue)).
- **A missing identity is a programming error and raises**, like an input of the wrong type, and before anything runs. The case it prevents: one code path of
  an application forgets `identity=`, and a tool runs as nobody. A union of the type and `None` (`identity_type=Teacher | None`) is how an application says that some runs (a nightly job) have none.
- `run_many` gives every input the same identity, and checks it **once, at the call, whatever the number of inputs** (also none). `run_job` and `submit` give it to the job: every agent the job runs with `ctx.run` has it.
- **A run refuses an identity that is not its job's.** When a run has a `job_id` and the job already has a run with another identity (or with none, and this one has one, or the reverse), the runner raises
  `ValueError` ("a job is for one identity") before anything is written, naming no identity value. `run` and `run_many` check at the call; `stream` is not `async`, so it checks when the loop starts, where it also
  raises for a `job_id` that does not exist. A run with no `job_id` is not checked. It is a guard against a mistake, not a security boundary: two first runs of one job with different identities at the same instant can both pass.
- **The worker's type must be one its runner accepts.** The worker rebuilds the identity from the message with its type and gives it to the run, so every model of the worker's type must be a model of
  the runner's (or a subclass of one), and if the worker allows no identity, so must the runner. `JobWorker` raises `TypeError` when it is built, naming both types, if not: otherwise every job that runs an
  agent would fail at its first `ctx.run`. The two types need not be equal: a worker for `Teacher` over a runner of `Teacher | None` is fine (the runner also serves runs in the process that have none),
  and a job can be a pipeline of several agents, because they all run with the job's one identity on the one runner. **The client's declaration is
  optional:** with it, `submit` checks the identity before it creates a job (`TypeError`); without it, `submit` accepts any Pydantic model and serialises it, and
  the worker is where the declared type is the authority.

## How tools read it

`RunContext` becomes generic and gets a field:

```python
@dataclass
class RunContext(Generic[IdentityT]):
    job_id: str | None
    attempt_id: str
    identity: IdentityT | None
```

A tool declares `runtime: ToolRuntime[RunContext[Teacher]]` and reads `runtime.context.identity`. LangChain fills the parameter in and hides it from the model, so
the model never passes it and cannot see it ([tools and the registry](tools-and-store.md#the-run-context)). A tool that does not declare `runtime` cannot see it.

- **The shared (MCP) tools** cannot take a `RunContext`, because one tool object serves many attempts. The runner sets a context variable around each attempt,
  as it does for the attempt's id, and code that cannot take `runtime` reads it: `CURRENT_IDENTITY`.
- **A tool on a runner that declares a union** annotates `RunContext[Teacher | Student]` and checks the kind (`isinstance`) before it acts. Nothing links the annotation to the runner's `identity_type`: a mismatch
  fails at run time, not in the editor.
- **Pipeline code** reads the identity of its job with `ctx.identity` on the `JobContext`, for a job that has to behave differently for a tenant.
- The identity is the **same object** that was passed, in a process. On a worker it is a new object, rebuilt from the message, and equal to it apart from the fields marked `exclude=True`, which have their default.

## Who inherits it

- **A launched agent** (`launch_agent`) runs with the identity of its parent, and so does an agent started in the background in the same process.
- **A background job on a worker** (a runner with `background=JobClient`) is submitted with the identity of its parent.
- **Both read it from the attempt that started them** (its `RunContext`): how a background job runs, here or on a worker, does not change where its identity comes from.
- **The model cannot set, change or drop it.** `launch_agent` and `start_background` take no identity argument.

**Why identity is inherited and capabilities are not.** A launched agent does not get its parent's tools, skills or sub-agents: each agent has the allow-lists its own
definition gives it, and that is least privilege. An identity is not a capability. It is *who the whole run acts for*, and a child that ran as nobody, or as
someone else, would make its tools act for the wrong person or for no one. So a child can never have more, or less, than its parent's identity: it is the
same one. What the child **can do** with it stays limited by its own allow-lists.

## Across the queue

A submitted job is the one place where a run leaves the process, so it is the one place where the identity is serialised, and the only place where it could be read or changed
by anything but the application.

- `JobMessage` gets two fields: `identity: dict[str, Any] | None`, the identity as JSON, from `model_dump(mode="json")`, without the fields marked `exclude=True`, and **`identity_kind: str | None`**, which model it is:
  the name of its class (`type(identity).__name__`). Both are `None` when the run has no identity, and the kind is sent **always**, also when the declared type is a single model.
  Two fields, and not one that holds both, so that a queue carries the same flat message as before.
- **The worker rebuilds it** with its declared type, and gives it to the run, so a tool sees on a worker what it sees in a process. It **chooses the model by the kind**, and validates the data against that one model:
  pydantic's own choice among the members of a union is the first one that fits, which is the wrong model when a teacher and a student have the same fields. A kind that no member of the worker's type has
  fails the job, so a message of a `Student` never passes as a `Teacher`, also when the worker declares a single `Teacher`.
- **It fails closed.** A message whose identity does not validate against the worker's type, one whose kind the worker's type does not have, one that has an identity and no kind, and a message without an identity when the worker's type does not allow `None`,
  make the job `FAILED` with `InvalidJobIdentity` (the way an input that does not validate does): the job is not run, and the worker does not retry it.
- **Every queue carries it,** and `testing.check_queue` checks that a queue returns the identity and its kind unchanged, so that a queue that drops a field fails the kit. `SqliteQueue`
  stores them in the `job_queue` table (two migrations: the identity, then its kind) until the message is acknowledged, on the same terms as the input.
- **The queue is trusted.** featherweight does not sign or encrypt a message: a deployment secures its queue (access control, TLS, encryption at rest) as it does for the
  input. An application that wants a signature or encryption of its own writes or wraps a queue: `JobQueue` is the seam, and a wrapper that signs the message on
  `enqueue` and checks it on `receive` is a [recipe](../recipes/a-queue-that-signs-its-messages.md), run through `check_queue` like any queue.

## Resuming a job

[Resuming](runner.md#resuming) reuses a step of an old job when the agent hash and the input hash match. That assumes a step depends only on its agent and its input. A tool that
reads the identity makes it depend on who ran it, and if the identity has changed, so may the permissions, so the old result cannot be relied on. So the identity takes part, as a hash:

- Each run records **`identity_hash`**: the SHA-256 of the identity as canonical JSON (keys sorted, no spaces: the form `input_hash` uses) of `{"kind": <the class name>, "data": <its fields>}`, without the fields
  marked `exclude=True`, so that it is the same in a process and on a worker. **`NULL` when the run had no identity.** The framework stores no identity.
  The kind is in it because two models can have the same fields: a `Teacher` and a `Student` with the same data are two identities, and a step of one is not a step of the other.
- **The hash is of the identity, never of the message.** It is computed from the identity object (its class and its fields): in a process from the object that was passed, on a worker from the object the worker
  rebuilt by the kind and the data. Because the worker chooses the model by the kind, both give the same class name and the same fields, and so the same hash. The two fields of the message are how
  the object travels; the `{"kind", "data"}` text is what is hashed, and it is never sent.
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
- **If you pass a `job_id` to `run`, `run_many` or `stream` yourself,** pass the same identity every time: a job is for one identity.
- **Keep it small,** and keep secrets out of it. Do not change an identity after a run has it.
- **Do not let users declare stdio MCP servers.** A `StdioMCPServer` runs a command on your server. An application that builds agents from what users write allows only
  `HttpMCPServer`, or servers from a list it controls.
- **The name of an identity class is a name in the queue.** The kind is the class name, so a rename of an identity class between `submit` and the worker running the job fails that job (`InvalidJobIdentity`),
  as a renamed job fails a queued job, and changes the `identity_hash` of the runs after it, so a resume reruns their steps once. Rename when the queue is empty, or keep the old name.
- **Names are the application's.** A job's name is a plain string it chooses, unique in what its [`jobs_lookup`](submitted-jobs.md#jobs-that-are-not-declared-in-code)
  searches, and stable: a rename breaks the jobs queued under the old name. The pattern is `^[A-Za-z][A-Za-z0-9_.-]*$`: a letter first, then letters, digits, `_`, `.` and `-`.

## Decisions

- **An identity is an argument of the run, not a field of the agent,** so the same agent runs for every user, and the hash of an agent says what it is, not who ran it.
- **Typed, declared, and required when declared:** the type says what a run must pass, and a union with `None` is how an application allows none. A missing identity raises,
  instead of letting a tool run as nobody.
- **It crosses the queue as a field of the message, rebuilt with the declared type,** and a message that does not fit fails the job. It is the one place where a run leaves the process.
- **The kind of an identity is the name of its class, sent in its own field, always.** A union of models is wanted (one agent serves a teacher or a student), and a plain JSON of the fields does not say which
  model it was: pydantic would rebuild a student as the first model that fits. The class name is the simplest kind that works; the cost, a rename of the class while a job is queued, is documented, not engineered
  around (the application names its jobs the same way). It is sent always, so there is one rule, and a worker that declared a single type does not accept the data of another model with the same fields.
  The hash includes it for the same reason.
- **The worker keeps its own type, and it must be compatible with its runner's.** The worker's type is what it accepts from the queue, the runner's what a run accepts; a worker may be stricter than its runner,
  never looser.
- **Inherited by what a run launches, and the model cannot touch it.** It is who the run is for, not a capability.
- **A job is for one identity, and the runner refuses a run that is not.** The design does not support several identities in one job (the key of a result is the job, the agent and the input), so the rule is stated and
  checked at run time, rather than the key changed. It is checked on each run that names a job: one indexed lookup of the run log.
- **An excluded field has a default,** because the worker rebuilds the identity without it, and a failure that shows only on a worker is found when the type is declared instead.
- **Identity, not credentials.** The framework has no credential provider and no protocol for one: where tokens live and how they are exchanged is the application's.
- **A hash on the run, so that resume reuses only the same step.** The framework keeps a hash, as it keeps `input_hash`, and never the identity.
- **No codec, no signing and no encryption in the framework.** The queue is the seam; a deployment secures it, and a wrapper queue is a recipe.
- **Plain strings for names, no key.** How an application names and keys what it defines is its own; the framework runs a job by the name it is given.

## Not supported

- **Authenticating a user,** deciding what they may do, and getting a credential for them.
- **An identity per tool call,** or a tool that changes it: a run has one.
- **Several identities in one job:** a job is for one identity. The result store's key does not carry the identity, and a resume finds a step by the job and the identity: a job that held two would hand one identity's
  output to the other.
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
- **Inheritance:** a launched agent and an in-process background job have it; a background job submitted to a worker carries it.
- **A job is for one identity:** a second run of a job for another identity raises before the model is called and writes nothing, for `run`, `run_many` (also with no inputs) and `stream`; the same identity twice is fine; none then an identity
  and the reverse are refused; a run with no `job_id` is not checked; a resume under another identity is a new job and works; the `Database` kit on sqlite and Postgres.
- **Excluded fields:** one with no default is refused when the runner, the worker and the client are built (for a model, and for a member of a union); a tool sees the value in a process and the default on a worker.
- **The kind:** a union of two models with the same fields is rebuilt as the model that was sent, in both orders of the union; a single declared type refuses the data of another model; a kind that the type does not have, and an
  identity with no kind, fail the job; two members with one class name are refused when built; the worker's type is checked against its runner's (accepted and refused cases).
- **The queue:** `check_queue` against `SqliteQueue` and `MemoryQueue` (a queue that drops a field fails it); a job submitted with an identity whose tool reads it on a worker; a
  message without an identity for a worker that requires one, and a message that does not validate, fail the job and are not run.
- **Resume:** the same identity reuses, another one reruns, `NULL` matches only `NULL`; the hash is the same in a process and on a worker, two models with the same data have two hashes, and a field marked `exclude=True` is not in it; the `Database`
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
