<!-- Built from docs/recipes by scripts/build_skill.py. Edit the pages there, not this file. -->

# Recipes

Patterns for building with `featherweight`. Each snippet is complete enough to adapt. The agent definitions are in `SKILL.md`. This is an abridged copy: each recipe links to its full page.

## A pipeline of two agents

A pipeline is a job: a function with a typed input and output. Order and branching are plain Python. Every agent it runs with `ctx`
belongs to the job, so they share its run log and its trace.

```python
from pydantic import BaseModel

from featherweight import AgentRunner, JobContext, RunStatus, SqliteDatabase, job, run_job


class Texts(BaseModel):
    texts: list[str]


class People(BaseModel):
    people: list[str]


@job
async def find_people(ctx: JobContext, input: Texts) -> People:
    results = await ctx.run_many(PeopleExtractor, [ExtractInput(text=t) for t in input.texts])
    people = []
    for result in results:
        if result.status == RunStatus.SUCCESS:  # decide here what a failed run means: skip, stop, ask again
            people.extend(result.output.people)
    return People(people=sorted(set(people)))


async def main():
    async with SqliteDatabase("featherweight.db") as db:  # or ":memory:" for no file
        outcome = await run_job(AgentRunner(db), find_people, Texts(texts=["Anna met Tom.", "Li came."]))
        # outcome.status, outcome.result (a People) on success, outcome.error on failure
```

`run_job` creates the job, runs it and records `SUCCESS` or `FAILED`. A job that raises comes back as a failed outcome, not an
exception. The same job can be submitted to a queue and run by a worker ([submitted jobs](submitted-jobs.md)).

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-pipeline-of-two-agents.md)

## Resuming a failed job

Create the first job with `resumable=True` (it keeps the outputs of its steps), and when it fails, run the same job again
and point it at the failed job:

```python
first = await run_job(runner, find_people, texts, resumable=True)
outcome = await run_job(runner, find_people, texts, resume_from=first.job_id)
```

Agent runs that succeeded with the same agent and input are answered from the kept results (`result.reused`, zero tokens). Keep
the code between agents deterministic (no clock, no `random`), and do not rely on state outside the result store for a resume.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/resuming-a-failed-job.md)

## Telling a retry what went wrong

A retry does not see the last error. If the next attempt should, make it part of the input:

```python
class FillInput(AgentInput):
    form: str
    problems: list[str] = []


result = await runner.run(Filler, FillInput(form=form), job_id=job_id)
for _ in range(2):
    if result.status == RunStatus.SUCCESS:
        break
    result = await runner.run(Filler, FillInput(form=form, problems=[result.error or ""]), job_id=job_id)
```

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/telling-a-retry-what-went-wrong.md)

## Testing with a fake model

No test makes a real model call. A fake is **model settings**, like a real provider: a `ModelSettings` subclass whose
`build()` returns a fake chat model. The test runs the same agent with its `model` replaced by the fake settings, and the
runner takes the path it takes in production, `agent.model.build()`. The structured reply is a tool call named after the
output class. This is a whole test file for the agent of [Getting started](https://github.com/aadhithya/featherweight/blob/main/docs/getting-started.md) (saved as `app.py`). Tests need
`asyncio_mode = auto` (in `pyproject.toml`, `[tool.pytest.ini_options]`) or `pytest -o asyncio_mode=auto`, and
`pytest-asyncio`.

```python
from typing import Any, ClassVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app import ExtractInput, PeopleExtractor  # your agent module
from featherweight import AgentRunner, ModelSettings, RunStatus, SqliteDatabase


class FakeSettings(ModelSettings):
    """Model settings that build the fake chat model they hold. Any provider's settings work the same way."""

    provider: ClassVar[str] = "fake"
    model: str = "fake-model"
    chat: Any = Field(default=None, exclude=True)  # the fake chat model; not part of the settings' data

    def build(self):
        return self.chat


class FakeModel(BaseChatModel):
    """Replies by what the input contains, so `run_many` gives each input its own reply. `answers` maps a piece of
    input text to the outputs to give, one for each call; the last one repeats."""

    answers: dict[str, list[dict]]
    calls: dict[str, int] = {}
    total: int = 0  # model calls

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = str(next(m.content for m in messages if m.type == "human"))
        key = next(k for k in self.answers if k in user)
        n = self.calls.get(key, 0)
        self.calls[key] = n + 1
        self.total += 1
        output = self.answers[key][min(n, len(self.answers[key]) - 1)]
        call = {"name": "ExtractOutput", "args": output, "id": f"call-{self.total}"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


async def run(tmp_path, answers, text="Anna came."):
    fake = FakeModel(answers=answers)  # one fake for all attempts, so its counters last

    class UnderTest(PeopleExtractor):  # the same agent, run by the fake
        model: ModelSettings = Field(default_factory=lambda: FakeSettings(chat=fake))

    async with SqliteDatabase(tmp_path / "test.db") as db:
        runner = AgentRunner(db, retry_delay_s=0)
        return await runner.run(UnderTest, ExtractInput(text=text)), fake


async def test_success(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Anna"]}]})
    assert result.status == RunStatus.SUCCESS and result.output.people == ["Anna"]


async def test_check_output_rejects_and_the_runner_retries(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Nobody"]}, {"people": ["Anna"]}]})
    assert result.status == RunStatus.SUCCESS and result.attempts == 2


async def test_a_run_that_keeps_failing_is_returned_as_failed(tmp_path):
    result, _ = await run(tmp_path, {"Anna": [{"people": ["Nobody"]}]})  # the last reply repeats
    assert result.status == RunStatus.FAILED and result.attempts == 3  # max_attempts
    assert "names not in the text" in (result.error or "")


async def test_a_reply_that_breaks_the_schema_is_fixed_inside_the_attempt(tmp_path):
    result, fake = await run(tmp_path, {"Anna": [{"people": "not a list"}, {"people": ["Anna"]}]})
    assert result.status == RunStatus.SUCCESS and result.attempts == 1 and fake.total == 2
```

A schema violation (a wrong type, a value outside a `Literal`) is sent back to the model, which gets a second chance in
the same attempt. A rejection by `check_output` fails the attempt, and the runner retries it, up to `max_attempts`. Use
a `Literal` or a validator for rules on the output alone, and `check_output` for rules between the input and the output.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/testing-with-a-fake-model.md)

## An agent with an MCP server

```python
from featherweight import HttpMCPServer


class RepoReader(AgentBase):
    name: str = "repo_reader"
    description: str = "Answers questions about a GitHub repository"
    version: str = "0.1.0"
    prompt: str = "Answer from the repository, using the tools."
    mcp_servers: list = [
        HttpMCPServer(
            name="github",
            url="https://api.githubcopilot.com/mcp/readonly",
            bearer_from="GITHUB_TOKEN",  # the NAME of the variable, not the token
            description="Repositories, issues and code",
        )
    ]
```

`async with AgentRunner(db) as runner:` keeps the server connections open between runs and closes them at the end. It is
optional.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/an-agent-with-an-mcp-server.md)

## An agent that launches another

```python
class Editor(AgentBase):
    name: str = "editor"
    description: str = "Writes a headline"
    version: str = "0.1.0"
    prompt: str = "Write a headline. Ask the summariser for a summary first."
    subagents: list[type] = [Summariser]  # classes: the only agents this one may launch
```

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/an-agent-that-launches-another.md)

## A streaming agent for chat

```python
from contextlib import aclosing

from featherweight import ChatInput, Claude, StreamingAgent
from featherweight.adapters import agui


class Support(StreamingAgent):
    name: str = "support"
    description: str = "Answers customers"
    version: str = "1"
    prompt: str = "You help customers. Be short."
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low", thinking="off")


run = runner.stream(Support, ChatInput.from_langchain([*history.messages, user_message]))
try:
    async with aclosing(agui.encode(run)) as pieces:  # SSE strings: send them with any web framework
        async for piece in pieces:
            yield piece
finally:
    history.add_messages([user_message, *run.new_messages])  # you keep the conversation; featherweight keeps none
```

`runner.cancel(run.run_id)` stops it. `featherweight.adapters.vercel` does the same for `useChat`. A chat in the terminal is
`examples/chat/cli.py`, and the same agent behind FastAPI is `examples/chat/api.py`; [the streaming guide](streaming.md) has the details. Test it with a fake streaming chat model: the
`Streams` double in the repository's `tests/fakes.py` shows how.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-chat-agent-that-streams.md)

## A result store of your own

Resuming a job reads the outputs of finished steps from a `ResultStore`. Subclass it, write `put`, `get` and `delete_job`, and
check it with the conformance kit (a missing method cannot be built; a broken rule raises `AssertionError`):

```python
from featherweight import AgentRunner, ResultKey, ResultStore, testing


class MyStore(ResultStore):
    async def put(self, key: ResultKey, output: dict) -> None: ...  # key: job_id, agent_hash, input_hash
    async def get(self, key: ResultKey) -> dict | None: ...
    async def delete_job(self, job_id: str) -> None: ...


await testing.check_result_store(lambda: MyStore())
runner = AgentRunner(db, results=MyStore())
```

The first job has to be created `resumable=True`. `examples/results/store.py` is a whole store (a JSON file per result).

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-result-store-of-your-own.md)

## A job that a worker runs

```python
@job
async def shout(ctx: JobContext, input: Text) -> Shouted:
    return Shouted(shouted=input.text.upper())


queue = SqliteQueue(db)
worker = JobWorker(db, queue, AgentRunner(db), jobs=[shout])  # its own process: await worker.run()
client = JobClient(db, queue)
job_id = await client.submit(shout, Text(text="hello"))
async for event in client.events(job_id):
    ...  # or sse.encode(...) from a web handler
print(await client.result(shout, job_id))
```

Run as many workers as you like: a worker that dies loses its lease, and another takes the job over (`executions` + 1; a resumable job reuses its finished steps).
A job must be safe to run twice, and `@job(timeout=...)` (600 s by default) stops one that hangs. [the submitted jobs guide](submitted-jobs.md) has cancelling, shutdown, leases and writing your own queue. `examples/jobs` runs it, and `takeover.py` kills a worker mid-job.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-job-that-a-worker-runs.md)

## Traces for jobs and agents

```python
from featherweight import AgentRunner, OTelObserver

runner = AgentRunner(
    db, observers=[OTelObserver()]
)  # pip install 'featherweight[otel]'; you configure the SDK and the exporter
```

A job becomes `invoke_workflow`, each attempt `invoke_agent`, with `chat` and `execute_tool` spans under it, in the GenAI conventions.
Content is off unless `OTelObserver(capture_content=True)`. Your own spans use the OpenTelemetry API and nest under the job.
[the observability guide](observability.md) has the rest, and `examples/observability` runs it.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/traces-for-jobs-and-agents.md)

## An agent that starts another and goes on

```python
class Researcher(AgentBase):
    background_jobs = [WebSearcher]  # the model gets `start_background`; the child is a job


runner = AgentRunner(db, results=store)  # here, in this process; needs a result store
runner = AgentRunner(db, results=store, background=client)  # or on a worker: a JobClient
message = await runner.background_message(job_id)  # a user message with the child's result, for the next turn
```

While the parent runs, a finished child's result is added before the next model call. A chat turn does not wait: follow `background_started` and
`background_finished` on its stream, or the child's job events, and start the next turn yourself. [the background jobs guide](background-agents.md) has the rest.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/an-agent-that-starts-another-and-goes-on.md)

## A different provider

```python
from featherweight import Claude


class Triager(AgentBase):
    ...
    model: Claude = Claude(model="claude-haiku-5-5", effort="low")
```

`pip install "featherweight[anthropic]"` and set `ANTHROPIC_API_KEY`.

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-different-provider.md)

## A skill from a database or S3

A skill is plain data, so it can live anywhere. Text in a database needs nothing; text in an object store is fetched when the agent asks.

```python
from featherweight import Skill

# the skill is a row of text in your database: build it from the rows, nothing to add
skill = Skill.from_markdown(row["skill_md"], resources={r["path"]: r["text"] for r in resource_rows})
```

```python
from featherweight import Skill


# the skill is in S3: fetch it when the agent asks, without blocking the event loop
class BucketSkill(Skill):
    etag: str  # changes when the text does: it is a field, so it is in the agent's hash

    async def aread(self, resource: str = "") -> str:
        return await bucket.get_text(f"{self.name}/{resource or 'SKILL.md'}")  # your client, awaited


skill = BucketSkill(name="meeting-notes", description="How this department writes meeting notes", etag=head.etag)
```

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-skill-from-a-database-or-s3.md)

## A skill for each department

The agent is written once, and each department's skills are data: build the agent class for the request, with that department's skills.

```python
from typing import ClassVar

from pydantic import Field, create_model

from featherweight import AgentBase, AgentInput, AgentOutput, Skill


class NotesInput(AgentInput):
    transcript: str


class NotesOutput(AgentOutput):
    notes: str = ""


class LectureNotes(AgentBase):
    name: str = "lecture_notes"
    description: str = "Turns a lecture transcript into notes"
    version: str = "1"
    prompt: str = "Write lecture notes from the transcript, in the style of the skill you have."
    input_model: ClassVar[type[AgentInput]] = NotesInput
    output_model: ClassVar[type[AgentOutput]] = NotesOutput


def for_department(skills: list[Skill]) -> type[LectureNotes]:
    """The same agent, with this department's skills."""
    return create_model(
        "DepartmentNotes", __base__=LectureNotes, skills=(list[Skill], Field(default_factory=lambda: skills))
    )


law = for_department([Skill.from_markdown(row["skill_md"]) for row in rows_of("law")])  # your database
result = await runner.run(law, NotesInput(transcript="..."))
```

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-skill-for-each-department.md)

## Agents built from JSON, with your own rules

Your users define agents in a form; you keep the definition as JSON and build the class when it is needed. The builder is yours, so the rules are too:
which tools a user may pick, which models, whether they may add skills, what every agent gets.

```python
from typing import ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import Field, create_model

from featherweight import AgentBase, AgentInput, AgentOutput, SetupError, Skill
from featherweight.model_settings import Gemini, ModelSettings

HOUSE_STYLE = Skill.from_markdown(house_style_md)  # injected into every agent: the users do not choose it
MODELS: dict[str, ModelSettings] = {"fast": Gemini()}  # the models a user may pick, by a name


@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


TOOLS: dict[str, BaseTool] = {"word_count": word_count}  # the catalog: the only tools a user can give an agent
TYPES = {"string": str, "integer": int, "boolean": bool}


def fields(base: type, name: str, spec: list[dict], default) -> type:
    return create_model(name, __base__=base, **{f["name"]: (TYPES[f["type"]], default(f)) for f in spec})


def build(definition: dict) -> type[AgentBase]:
    """A user's definition, as an agent class. What it may not contain is refused here, before anything runs."""
    if definition.get("skills"):
        raise SetupError("users cannot add skills")
    unknown = [t for t in definition.get("tools", []) if t not in TOOLS]
    if unknown:
        raise SetupError(f"not in the catalog: {unknown}")
    title = "".join(part.title() for part in definition["id"].split("_"))
    In = fields(AgentInput, f"{title}Input", definition["input"], lambda f: ...)
    Out = fields(AgentOutput, f"{title}Output", definition["output"], lambda f: TYPES[f["type"]]())
    cls = create_model(
        title,
        __base__=AgentBase,
        name=(str, definition["id"]),  # your unique key: it is the job's name on a worker
        description=(str, definition["description"]),
        version=(str, str(definition["version"])),
        prompt=(str, definition["prompt"]),
        model=(ModelSettings, Field(default_factory=lambda: MODELS[definition["model"]])),
        tools=(list[BaseTool], Field(default_factory=lambda: [TOOLS[t] for t in definition.get("tools", [])])),
        skills=(list[Skill], Field(default_factory=lambda: [HOUSE_STYLE])),
    )
    cls.input_model, cls.output_model = In, Out
    return cls


Summariser = build(definition_from_your_database)
result = await runner.run(Summariser, Summariser.input_model(text="..."))
```

On a worker, the agent does not exist in the code that started it. `jobs_lookup` is the fallback that finds it, from the same definitions:

```python
from featherweight import JobWorker


async def jobs_lookup(name: str, job_hash: str | None, identity):
    definition = await definitions.get(identity.tenant, name)  # your storage; the identity scopes the search
    return build(definition) if definition else None


worker = JobWorker(db, queue, runner, jobs=[], jobs_lookup=jobs_lookup, identity_type=Teacher)
```

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/agents-built-from-json.md)

## A queue that signs its messages

featherweight trusts its queue: a message says who a run is for, and whoever can write to the queue can write that. Secure the queue the way you secure any broker (access control, TLS, encryption at rest). If you
want a signature of your own too, wrap a queue: `JobQueue` is the seam, and a wrapper is a queue like any other.

```python
import hashlib
import hmac
import json
import logging
import time

from featherweight.queue import Delivery, JobMessage, JobQueue

logger = logging.getLogger(__name__)
SIGNATURE = "x-signature"  # a key of `trace_context`: the one map of strings that every message carries


class HmacQueue(JobQueue):
    """Signs a message when it goes in, and drops one that does not carry the signature of its content when it comes out."""

    def __init__(self, inner: JobQueue, key: bytes):
        self.inner, self.key = inner, key

    def _sign(self, message: JobMessage) -> str:
        trace = {k: v for k, v in message.trace_context.items() if k != SIGNATURE}
        body = message.model_copy(update={"trace_context": trace}).model_dump(mode="json")
        return hmac.new(self.key, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    async def enqueue(self, message: JobMessage) -> None:
        signed = {**message.trace_context, SIGNATURE: self._sign(message)}
        await self.inner.enqueue(message.model_copy(update={"trace_context": signed}))

    async def receive(self, timeout: float) -> Delivery | None:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            delivery = await self.inner.receive(left)
            if delivery is None:
                return None
            message = delivery.message
            plain = message.model_copy(
                update={"trace_context": {k: v for k, v in message.trace_context.items() if k != SIGNATURE}}
            )
            if hmac.compare_digest(message.trace_context.get(SIGNATURE, ""), self._sign(plain)):
                return delivery.model_copy(update={"message": plain})  # as it was put in
            logger.warning("message_dropped job_id=%s: its signature is not valid", message.job_id)
            await self.inner.nack(
                delivery, requeue=False
            )  # the job row is not touched: a forged message may name a real job
        return None

    async def ack(self, delivery: Delivery) -> None:
        await self.inner.ack(delivery)

    async def nack(self, delivery: Delivery, *, requeue: bool) -> None:
        await self.inner.nack(delivery, requeue=requeue)

    async def extend(self, delivery: Delivery) -> None:
        await self.inner.extend(delivery)
```

Check it like any queue, with the kit, and give the client and the worker the wrapper, with the same key:

```python
from featherweight import JobClient, JobWorker, testing
from featherweight.queue import SqliteQueue
from featherweight.testing import MemoryQueue

await testing.check_queue(lambda lease: HmacQueue(MemoryQueue(lease=lease), key=b"a secret from your vault"))

queue = HmacQueue(SqliteQueue(db), key)
client, worker = JobClient(db, queue), JobWorker(db, queue, runner, jobs=[...])
```

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-queue-that-signs-its-messages.md)

## A tool that gets a token for its identity

A tool acts for the user whose identity the run has. featherweight carries the identity; the tool checks what it may do, and gets a credential with code of your own, at each call.

```python
from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel

from featherweight import AgentRunner, RunContext


class Teacher(BaseModel):
    user_id: str
    tenant: str


def send_email_tool(tokens, directory):
    """`tokens` and `directory` are yours: they are given to the tool when your application starts."""

    @tool
    async def send_email(to: str, subject: str, body: str, runtime: ToolRuntime[RunContext[Teacher]]) -> str:
        """Send an email as the signed-in teacher."""
        who = runtime.context.identity
        if who is None or not await directory.may(
            who.user_id, "send_email"
        ):  # re-checked now: the identity is a snapshot
            return "Not allowed."
        token = await tokens.access_token(who.user_id, scope="mail.send")  # at each call: a long job outlives a token
        await mail_api.send(token, to, subject, body)
        return "Sent."

    return send_email


runner = AgentRunner(db, identity_type=Teacher)
result = await runner.run(Mailer, MailerInput(...), identity=Teacher(user_id="u1", tenant="uni"))
```

[The full recipe](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/a-tool-that-gets-a-token-for-its-identity.md)
