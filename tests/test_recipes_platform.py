"""The recipes for platform builders: their code blocks run, against fakes. What a recipe shows is what works."""

import ast
import asyncio
import inspect
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import Scripted, runner_with, structured_reply, tool_reply
from langchain_core.messages import SystemMessage, ToolMessage
from pydantic import BaseModel

from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, SetupError, Skill, testing
from featherweight.client import JobClient
from featherweight.events import wait_for_job
from featherweight.fingerprint import agent_hash
from featherweight.model_settings import Gemini
from featherweight.models import RunStatus
from featherweight.queue import JobMessage, SqliteQueue
from featherweight.testing import MemoryQueue, MemoryResultStore
from featherweight.worker import JobWorker

RECIPES = Path(__file__).parent.parent / "docs" / "recipes"


def blocks(page: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", (RECIPES / page).read_text(), re.S)


async def run_block(code: str, namespace: dict[str, Any]) -> None:
    """Run a block as the reader would: with top-level `await` allowed, in a namespace that has what the recipe says is yours."""
    namespace.setdefault("__name__", "recipe")  # `create_model` names the module of a class after the caller's
    compiled = compile(code, "<recipe>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    result = eval(compiled, namespace)
    if inspect.iscoroutine(result):
        await result


def fake_model(monkeypatch, script: list) -> Scripted:
    """Every agent in a recipe has the model its code names (`Gemini()`): replace what it builds with a scripted fake."""
    fake = Scripted(script=script)
    monkeypatch.setattr(Gemini, "build", lambda self: fake)
    return fake


SKILL_MD = (
    "---\nname: meeting-notes\ndescription: How this department writes meeting notes\n---\nUse short sentences.\n"
)


# a skill from a database or S3


async def test_a_skill_from_rows_and_a_skill_fetched_late_from_a_bucket(db):
    database_block, bucket_block = blocks("a-skill-from-a-database-or-s3.md")
    row = {"skill_md": SKILL_MD}
    resource_rows = [{"path": "references/tags.md", "text": "#decision"}]
    ns: dict[str, Any] = {"row": row, "resource_rows": resource_rows}
    await run_block(database_block, ns)
    assert ns["skill"].read("references/tags.md") == "#decision" and ns["skill"].name == "meeting-notes"

    files = {"meeting-notes/SKILL.md": "From the bucket.", "meeting-notes/references/tags.md": "#from-bucket"}

    class Bucket:
        async def get_text(self, key: str) -> str:
            await asyncio.sleep(0)
            return files[key]

    ns = {"bucket": Bucket(), "head": SimpleNamespace(etag="v1")}
    await run_block(bucket_block, ns)
    bucket_skill = ns["skill"]
    assert (
        await bucket_skill.aread() == "From the bucket."
        and await bucket_skill.aread("references/tags.md") == "#from-bucket"
    )

    class Agent(AgentBase):
        name: str = "a"
        description: str = "a"
        version: str = "1"
        prompt: str = "p"
        skills: list[Skill] = [bucket_skill]

    ns["head"].etag = "v2"
    await run_block(bucket_block, ns)

    class Edited(Agent):
        skills: list[Skill] = [ns["skill"]]

    assert agent_hash(Agent()) != agent_hash(Edited())  # an edit in the store reaches the hash through the etag


async def test_a_bad_skill_is_refused_before_it_is_saved():
    with pytest.raises(ValueError):
        Skill.from_markdown("no frontmatter")  # what the page says a web editor should do first


# a skill for each department


class NotesInput(AgentInput):
    transcript: str


async def test_one_agent_with_the_skills_of_each_department(db, monkeypatch):
    (block,) = blocks("a-skill-for-each-department.md")
    fake = fake_model(monkeypatch, [structured_reply("NotesOutput", {"notes": "ok"})] * 2)
    runner = runner_with(db, fake)
    rows = {
        "law": [{"skill_md": SKILL_MD.replace("meeting-notes", "notes-style").replace("meeting", "law")}],
        "bio": [{"skill_md": SKILL_MD.replace("meeting-notes", "notes-style").replace("meeting", "biology")}],
    }
    ns: dict[str, Any] = {"rows_of": lambda dept: rows[dept], "runner": runner}
    # the block ends with a run for the law department; the biology one is built the same way
    await run_block(block, ns)
    assert ns["result"].status == RunStatus.SUCCESS
    bio = ns["for_department"]([Skill.from_markdown(rows["bio"][0]["skill_md"])])
    law = ns["law"]
    assert agent_hash(law()) != agent_hash(bio())  # two departments are two agents
    assert agent_hash(law()) == agent_hash(ns["for_department"]([Skill.from_markdown(rows["law"][0]["skill_md"])])())
    prompt = {m.content for messages in fake.seen for m in messages if isinstance(m, SystemMessage)}
    assert any("law" in p and "notes-style" in p for p in prompt)  # the model was told about the department's skill


# agents built from JSON


DEFINITION = {
    "id": "summariser",
    "description": "Summarises a text",
    "version": 1,
    "prompt": "Summarise the text.",
    "model": "fast",
    "tools": ["word_count"],
    "input": [{"name": "text", "type": "string"}],
    "output": [{"name": "summary", "type": "string"}],
}


async def test_agents_are_built_from_json_with_the_rules_of_the_builder(db, monkeypatch):
    build_block, worker_block = blocks("agents-built-from-json.md")
    fake = fake_model(
        monkeypatch,
        [tool_reply("word_count", {"text": "a b c"}, "t1"), structured_reply("SummariserOutput", {"summary": "three"})],
    )
    runner = runner_with(db, fake)
    ns: dict[str, Any] = {"house_style_md": SKILL_MD, "definition_from_your_database": DEFINITION, "runner": runner}
    await run_block(build_block, ns)

    result = ns["result"]
    assert result.status == RunStatus.SUCCESS and result.output.summary == "three"
    build = ns["build"]
    agent = ns["Summariser"]()
    assert [s.name for s in agent.skills] == ["meeting-notes"] and [t.name for t in agent.tools] == ["word_count"]
    with pytest.raises(SetupError, match="not in the catalog"):
        build({**DEFINITION, "tools": ["send_email"]})  # a tool outside the catalog
    with pytest.raises(SetupError, match="cannot add skills"):
        build({**DEFINITION, "skills": [{"name": "x"}]})
    with pytest.raises(KeyError):
        build({**DEFINITION, "model": "unlisted"})()  # a model that is not on the list is never built

    # and on a worker, where the class is not in the code that started it: the lookup finds it from the same definitions
    class Teacher(BaseModel):
        user_id: str
        tenant: str

    class Definitions:
        async def get(self, tenant: str, name: str) -> dict | None:
            return DEFINITION if (tenant, name) == ("uni", "summariser") else None

    queue, results = MemoryQueue(), MemoryResultStore()
    fake.script = [structured_reply("SummariserOutput", {"summary": "on a worker"})]
    fake.calls = 0
    worker_runner = AgentRunner(db, results=results, identity_type=Teacher)
    ns.update(definitions=Definitions(), db=db, queue=queue, runner=worker_runner, JobWorker=JobWorker, Teacher=Teacher)
    await run_block(worker_block, ns)
    worker = ns["worker"]
    client = JobClient(db, queue, results)
    job_id = await client.submit(
        ns["Summariser"], ns["Summariser"].input_model(text="x"), identity=Teacher(user_id="u1", tenant="uni")
    )
    task = asyncio.create_task(worker.run())
    try:
        row = await wait_for_job(db, job_id, timeout=10)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(task, timeout=5)
    assert row.status is RunStatus.SUCCESS  # the worker had `jobs=[]`: the lookup built the agent


# a queue that signs its messages


KEY = b"a secret from your vault"


def namespace_for_the_queue(db) -> dict[str, Any]:
    return {
        "testing": testing,
        "MemoryQueue": MemoryQueue,
        "SqliteQueue": SqliteQueue,
        "db": db,
        "key": KEY,
        "JobClient": JobClient,
        "JobWorker": JobWorker,
        "runner": None,
    }


async def test_the_signing_queue_passes_the_kit(db):
    class_block, use_block = blocks("a-queue-that-signs-its-messages.md")
    ns = namespace_for_the_queue(db)
    await run_block(class_block, ns)
    kit_line = [part for part in use_block.split("\n\n") if part.startswith("await testing.check_queue")][0]
    await run_block(kit_line, ns)  # check_queue(lambda lease: HmacQueue(MemoryQueue(lease=lease), key=...))


async def test_the_signing_queue_hands_over_what_was_put_in_and_drops_the_rest(db, caplog):
    class_block, use_block = blocks("a-queue-that-signs-its-messages.md")
    ns = namespace_for_the_queue(db)
    await run_block(class_block, ns)
    inner = MemoryQueue()
    queue = ns["HmacQueue"](inner, KEY)
    message = JobMessage(
        job_id="j1",
        job_name="shout",
        input={"text": "x"},
        identity={"user_id": "u1"},
        trace_context={"traceparent": "00-a"},
    )

    await queue.enqueue(message)
    delivery = await queue.receive(1.0)
    assert delivery is not None and delivery.message == message  # unchanged: the signature is not there to be seen
    await queue.ack(delivery)

    # messages that someone else put on the inner queue: not signed, signed with another key, or changed after signing
    other, scratch = ns["HmacQueue"](MemoryQueue(), b"another key"), MemoryQueue()
    await other.enqueue(message)
    forged = await other.inner.receive(1.0)
    assert forged is not None
    await ns["HmacQueue"](scratch, KEY).enqueue(message)
    signed = await scratch.receive(1.0)
    assert signed is not None and signed.message.trace_context["x-signature"]
    await inner.enqueue(message)  # no signature
    await inner.enqueue(forged.message)  # another key
    await inner.enqueue(
        signed.message.model_copy(update={"identity": {"user_id": "admin"}})
    )  # changed after it was signed

    with caplog.at_level(logging.WARNING):
        assert await queue.receive(0.2) is None  # all three are dropped
    assert caplog.text.count("message_dropped") == 3
    assert await inner.receive(0.05) is None  # and they are gone, not retried


async def test_the_signing_queue_runs_a_job_for_a_client_and_a_worker_that_share_the_key(db):
    class_block, use_block = blocks("a-queue-that-signs-its-messages.md")
    ns = namespace_for_the_queue(db)
    await run_block(class_block, ns)
    queue = ns["HmacQueue"](MemoryQueue(), KEY)
    from test_worker import Shout

    job_id = await db.create_job("shout", job_hash=Shout.job_hash())
    await queue.enqueue(JobMessage(job_id=job_id, job_name="shout", input={"text": "hi"}))
    worker = JobWorker(db, queue, AgentRunner(db), jobs=[Shout], results=MemoryResultStore(), receive_timeout=0.05)
    task = asyncio.create_task(worker.run())
    try:
        row = await wait_for_job(db, job_id, timeout=5)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(task, timeout=5)
    assert row.status is RunStatus.SUCCESS


# a tool that gets a token for its identity


class MailerInput(AgentInput):
    text: str = ""


class MailerOutput(AgentOutput):
    answer: str | None = None


class Directory:
    def __init__(self, allowed: set[str]):
        self.allowed = allowed

    async def may(self, user_id: str, action: str) -> bool:
        return user_id in self.allowed and action == "send_email"


class Tokens:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def access_token(self, user_id: str, scope: str) -> str:
        self.asked.append((user_id, scope))
        return f"token-for-{user_id}"


async def run_the_tool(db, monkeypatch, user_id: str, allowed: set[str]):
    (block,) = blocks("a-tool-that-gets-a-token-for-its-identity.md")
    head, tail = block.split("runner = AgentRunner(")
    sent: list[tuple] = []

    class MailApi:
        async def send(self, token, to, subject, body):
            sent.append((token, to))

    tokens, directory = Tokens(), Directory(allowed)
    ns: dict[str, Any] = {"mail_api": MailApi()}
    await run_block(head, ns)

    class Mailer(AgentBase):
        name: str = "mailer"
        description: str = "Sends mail"
        version: str = "1"
        prompt: str = "Send the mail."
        tools: list = [ns["send_email_tool"](tokens, directory)]
        input_model = MailerInput
        output_model = MailerOutput

    fake = fake_model(
        monkeypatch,
        [
            tool_reply("send_email", {"to": "a@x", "subject": "s", "body": "b"}, "t1"),
            tool_reply("send_email", {"to": "b@x", "subject": "s", "body": "b"}, "t2"),
            structured_reply("MailerOutput", {"answer": "done"}),
        ],
    )
    monkeypatch.setattr(Gemini, "build", lambda self: fake)
    ns.update(db=db, Mailer=Mailer, MailerInput=lambda *a, **k: MailerInput(), SimpleNamespace=SimpleNamespace)
    runner_with(db, fake)
    tail = tail.replace("Mailer, MailerInput(...)", "Mailer, MailerInput()").replace(
        'user_id="u1"', f'user_id="{user_id}"'
    )
    await run_block("runner = AgentRunner(" + tail, ns)
    texts = [m.content for m in fake.seen[-1] if isinstance(m, ToolMessage)]
    return ns["result"], tokens, sent, texts


async def test_the_tool_checks_the_identity_and_gets_a_token_at_each_call(db, monkeypatch):
    result, tokens, sent, texts = await run_the_tool(db, monkeypatch, "u1", {"u1"})

    assert result.status == RunStatus.SUCCESS
    assert tokens.asked == [("u1", "mail.send"), ("u1", "mail.send")]  # at each call, for this user
    assert sent == [("token-for-u1", "a@x"), ("token-for-u1", "b@x")]
    assert texts == ["Sent.", "Sent."]


async def test_the_tool_refuses_in_text_when_the_user_may_not_and_asks_for_no_token(db, monkeypatch):
    result, tokens, sent, texts = await run_the_tool(
        db, monkeypatch, "u1", set()
    )  # the role was revoked since the job started

    assert result.status == RunStatus.SUCCESS  # the run goes on
    assert tokens.asked == [] and sent == []
    assert texts == ["Not allowed.", "Not allowed."]
