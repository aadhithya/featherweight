"""The identity across the queue: a client puts it in the message, and the worker rebuilds it. Fake models only."""

import asyncio
import json
import logging
import sqlite3
from typing import Any, ClassVar

import pytest
from fakes import FakeSettings, Scripted, runner_with, structured_reply, tool_reply
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field
from test_worker import In, Out, Running, final, make

from featherweight.client import JobClient
from featherweight.jobs import Job, JobContext
from featherweight.migrations import MIGRATIONS
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, Status
from featherweight.queue import JobMessage, SqliteQueue
from featherweight.runner import AgentRunner
from featherweight.sqlite import SqliteDatabase
from featherweight.testing import MemoryQueue, MemoryResultStore, check_queue
from featherweight.worker import JobWorker


class Teacher(BaseModel):
    user_id: str
    tenant: str = "uni"
    token: str = Field(default="", exclude=True)  # a value that exists only in the process that made it


class WhoAmI(Job):
    name = "whoami"
    version = "1"
    input_model = In
    output_model = Out
    seen: ClassVar[list[Any]] = []

    async def run(self, ctx: JobContext, input: In) -> Out:
        WhoAmI.seen.append(ctx.identity)
        return Out(shouted=input.text.upper())


@pytest.fixture(autouse=True)
def clean():
    WhoAmI.seen.clear()


@pytest.fixture(params=["memory", "sqlite"])
def queue(request, db):
    return MemoryQueue() if request.param == "memory" else SqliteQueue(db)


def runner_for(db, identity_type=Teacher):
    return runner_with(db, Scripted(script=[]), identity_type=identity_type)


async def client_for(db, queue, **kwargs):
    return JobClient(db, queue, MemoryResultStore(), **kwargs)


# a job on a worker


async def test_a_job_submitted_with_an_identity_runs_with_one_that_is_equal_to_it(db, queue):
    client = await client_for(db, queue, identity_type=Teacher)
    teacher = Teacher(user_id="a", token="only in the submitting process")
    job_id = await client.submit(WhoAmI, In(text="x"), identity=teacher)

    async with Running(make(db, queue, [WhoAmI], runner=runner_for(db), identity_type=Teacher)):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS
    (seen,) = WhoAmI.seen
    assert isinstance(seen, Teacher) and seen is not teacher  # rebuilt from the message, not the same object
    assert seen == Teacher(user_id="a", tenant="uni", token="")  # without the field that is excluded


async def test_the_message_holds_the_identity_as_json_without_its_excluded_fields(db):
    queue = MemoryQueue()
    client = await client_for(db, queue)
    await client.submit(WhoAmI, In(text="x"), identity=Teacher(user_id="a", token="secret"))

    delivery = await queue.receive(1.0)

    assert delivery is not None and delivery.message.identity == {"user_id": "a", "tenant": "uni"}
    assert "secret" not in delivery.message.model_dump_json()


async def test_a_job_without_an_identity_has_none_when_the_worker_allows_it(db, queue):
    client = await client_for(db, queue)
    job_id = await client.submit(WhoAmI, In(text="x"))

    async with Running(make(db, queue, [WhoAmI], runner=runner_for(db, Teacher | None), identity_type=Teacher | None)):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS and WhoAmI.seen == [None]


# a message that does not fit the worker


async def message_for(db, queue, identity):
    job_input = In(text="x")
    job_id = await db.create_job("whoami", job_hash=WhoAmI.job_hash())
    await queue.enqueue(JobMessage(job_id=job_id, job_name="whoami", input=job_input.model_dump(), identity=identity))
    return job_id


async def test_a_message_without_an_identity_fails_a_worker_that_requires_one_and_is_not_run(db, queue):
    job_id = await message_for(db, queue, None)

    async with Running(make(db, queue, [WhoAmI], runner=runner_for(db), identity_type=Teacher)):
        row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "InvalidJobIdentity"
    assert WhoAmI.seen == []  # it did not run
    assert await queue.receive(0.05) is None  # and the message is acknowledged: it is not retried


async def test_an_identity_that_does_not_fit_the_declared_type_fails_the_job_and_its_values_are_not_logged(
    db, queue, caplog
):
    job_id = await message_for(db, queue, {"user_id": {"value": "secret-value-123"}})

    with caplog.at_level(logging.DEBUG):
        async with Running(make(db, queue, [WhoAmI], runner=runner_for(db), identity_type=Teacher)):
            row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "InvalidJobIdentity"
    assert WhoAmI.seen == []
    assert "secret-value-123" not in caplog.text
    assert "user_id" in caplog.text  # the field is named, the value is not


async def test_an_identity_in_a_message_fails_a_worker_that_declares_no_type(db, queue):
    job_id = await message_for(db, queue, {"user_id": "a"})

    async with Running(make(db, queue, [WhoAmI])):
        row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "InvalidJobIdentity" and WhoAmI.seen == []


# the client


async def test_a_client_with_a_declared_type_checks_the_identity_before_it_creates_a_job(db):
    queue = MemoryQueue()
    client = await client_for(db, queue, identity_type=Teacher)

    with pytest.raises(TypeError, match="every run needs identity="):
        await client.submit(WhoAmI, In(text="x"))
    with pytest.raises(TypeError, match="identity must be a Teacher"):
        await client.submit(WhoAmI, In(text="x"), identity=In(text="not a teacher"))

    assert await queue.receive(0.05) is None  # nothing was queued


async def test_a_client_without_a_declared_type_takes_any_model_and_nothing_else(db):
    queue = MemoryQueue()
    client = await client_for(db, queue)

    with pytest.raises(TypeError, match="identity must be a Pydantic model"):
        await client.submit(WhoAmI, In(text="x"), identity="u-1")

    await client.submit(WhoAmI, In(text="x"), identity=Teacher(user_id="a"))
    delivery = await queue.receive(1.0)
    assert delivery is not None and delivery.message.identity == {"user_id": "a", "tenant": "uni"}


# a background job on a worker


class StarterInput(AgentInput):
    text: str = ""


class StarterOutput(AgentOutput):
    answer: str | None = None


class Starter(AgentBase):
    name: str = "starter"
    description: str = "Starts a job in the background"
    version: str = "1"
    prompt: str = "Start it."
    model: ModelSettings = FakeSettings()
    background_jobs: list[type] = [WhoAmI]
    input_model: ClassVar[type[AgentInput]] = StarterInput
    output_model: ClassVar[type[AgentOutput]] = StarterOutput


def started_job_id(model) -> str:
    """The id of the job that `start_background` returned to the model."""
    for messages in model.seen:
        for message in messages:
            if isinstance(message, ToolMessage) and "job_id" in str(message.content):
                return json.loads(message.content)["job_id"]
    raise AssertionError("the model was not told the id of a job")


async def test_a_job_that_an_agent_starts_in_the_background_on_a_worker_runs_for_the_identity_of_its_parent(db):
    queue, results = MemoryQueue(), MemoryResultStore()
    client = JobClient(db, queue, results)
    worker_runner = AgentRunner(db, results=results, identity_type=Teacher)
    worker = JobWorker(
        db, queue, worker_runner, jobs=[WhoAmI], results=results, receive_timeout=0.05, identity_type=Teacher
    )
    running = asyncio.create_task(worker.run())
    try:
        parent = Scripted(
            script=[
                tool_reply("start_background", {"agent_name": "whoami", "input_json": '{"text": "x"}'}, "t-start"),
                structured_reply("StarterOutput", {"answer": "started"}),
                structured_reply(
                    "StarterOutput", {"answer": "started"}
                ),  # a child that ends first comes back as a message
            ]
        )
        runner = runner_with(db, parent, identity_type=Teacher, background=client, results=results)

        result = await runner.run(Starter, StarterInput(), identity=Teacher(user_id="boss", token="secret"))
        row = await client.wait(started_job_id(parent), timeout=10)
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)

    assert result.status == Status.SUCCESS and row.status is Status.SUCCESS
    assert WhoAmI.seen == [Teacher(user_id="boss")]  # the child ran on a worker, for whom its parent ran


# the kit


async def test_a_queue_that_drops_the_identity_fails_the_kit():
    class Dropping(MemoryQueue):
        async def enqueue(self, message: JobMessage) -> None:
            await super().enqueue(message.model_copy(update={"identity": None}))

    with pytest.raises(AssertionError, match="identity"):
        await check_queue(lambda lease: Dropping(lease=lease))


async def test_a_queue_table_from_before_the_column_is_migrated_and_its_waiting_message_has_no_identity(
    tmp_path, monkeypatch
):
    path = tmp_path / "old.db"
    monkeypatch.setattr("featherweight.sqlite.MIGRATIONS", MIGRATIONS[:2])  # a database from before the column
    async with SqliteDatabase(path):
        pass
    monkeypatch.undo()
    conn = sqlite3.connect(path)
    assert "identity" not in {r[1] for r in conn.execute("PRAGMA table_info(job_queue)")}
    conn.execute(
        "INSERT INTO job_queue (job_id, job_name, input, trace_context, state, enqueued_at) "
        "VALUES ('old-job', 'whoami', '{\"text\": \"x\"}', '{}', 'ready', '2026-11-20T00:00:00')"
    )
    conn.commit()
    conn.close()

    async with SqliteDatabase(path) as db:
        delivery = await SqliteQueue(db).receive(1.0)

    assert delivery is not None and delivery.message.job_id == "old-job" and delivery.message.identity is None
