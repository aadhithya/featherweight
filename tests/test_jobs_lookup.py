"""`jobs_lookup`: the worker's fallback for jobs that are not declared in code. Fake models only."""

import logging
from typing import Any

import pytest
from fakes import Echo, Reactive, respond, runner_with
from pydantic import BaseModel
from test_worker import In, Out, Running, Shout, final, make, submit

from featherweight.fingerprint import input_hash
from featherweight.jobs import as_job
from featherweight.models import Status
from featherweight.queue import JobMessage, SqliteQueue
from featherweight.results import ResultKey
from featherweight.testing import MemoryQueue, MemoryResultStore


class Teacher(BaseModel):
    user_id: str


class ShoutV2(Shout):
    """The same job, changed: a user edited the definition while the job waited."""

    version = "2"
    ran = False

    async def run(self, ctx: Any, input: In) -> Out:
        ShoutV2.ran = True
        return Out(shouted="v2")


@pytest.fixture(params=["memory", "sqlite"])
def queue(request, db):
    return MemoryQueue() if request.param == "memory" else SqliteQueue(db)


class Lookup:
    """A `jobs_lookup` that records what it was asked, and answers from a dict of class by name."""

    def __init__(self, known: dict[str, Any] | None = None, raises: Exception | None = None):
        self.known = known or {}
        self.raises = raises
        self.calls: list[tuple[str, str | None, Any]] = []

    async def __call__(self, name: str, job_hash: str | None, identity: Any):
        self.calls.append((name, job_hash, identity))
        if self.raises is not None:
            raise self.raises
        return self.known.get(name)


async def enqueue(db, queue, name: str, hash_of, text: str = "hi", identity: dict | None = None) -> str:
    """What a client does for a job whose recorded hash is `hash_of`, but under any name."""
    job_id = await db.create_job(name, job_hash=hash_of, input_hash=input_hash(In(text=text)))
    await queue.enqueue(
        JobMessage(
            job_id=job_id,
            job_name=name,
            input=In(text=text).model_dump(mode="json"),
            identity=identity,
            identity_kind="Teacher" if identity is not None else None,
        )
    )
    return job_id


# the fallback


async def test_the_lookup_is_not_called_for_a_name_jobs_has(db, queue):
    lookup = Lookup({"shout": ShoutV2})  # it would answer differently, if it were asked
    job_id = await submit(db, queue, Shout, "hello")

    async with Running(make(db, queue, [Shout], results=MemoryResultStore(), jobs_lookup=lookup)):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS and lookup.calls == []
    assert ShoutV2.ran is False


async def test_a_name_jobs_lacks_is_looked_up_and_the_job_runs_when_jobs_is_empty(db, queue):
    store = MemoryResultStore()
    lookup = Lookup({"shout": Shout})
    job_id = await submit(db, queue, Shout, "hello")

    async with Running(make(db, queue, [], results=store, jobs_lookup=lookup)):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS
    assert lookup.calls == [("shout", Shout.job_hash(), None)]  # the name, the hash recorded at submit, and no identity
    key = ResultKey(job_id=job_id, agent_hash=Shout.job_hash(), input_hash=input_hash(In(text="hello")))
    assert await store.get(key) == {"shouted": "HELLO"}


async def test_an_agent_class_is_a_job_too(db, queue):
    job_id = await submit(db, queue, as_job(Echo), "ok-1")
    runner = runner_with(db, Reactive(respond=respond, delay_s=0))

    async with Running(make(db, queue, [], runner=runner, jobs_lookup=Lookup({"echo": Echo}))):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS


def runner_for(db):
    return runner_with(db, Reactive(respond=lambda *_: RuntimeError("no model"), delay_s=0), identity_type=Teacher)


async def test_the_identity_the_worker_rebuilt_reaches_the_lookup(db, queue):
    lookup = Lookup({"shout": Shout})
    job_id = await enqueue(db, queue, "shout", Shout.job_hash(), identity={"user_id": "u-1"})

    async with Running(make(db, queue, [], jobs_lookup=lookup, identity_type=Teacher, runner=runner_for(db))):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS
    assert lookup.calls == [("shout", Shout.job_hash(), Teacher(user_id="u-1"))]


@pytest.mark.parametrize(
    "identity",
    [{"user_id": {"value": "secret-value-123"}}, None],
    ids=["an identity that does not fit the type", "no identity, for a type that requires one"],
)
async def test_a_message_whose_identity_does_not_fit_fails_before_the_lookup_is_asked(db, queue, identity, caplog):
    lookup = Lookup({"shout": Shout})
    job_id = await enqueue(db, queue, "shout", Shout.job_hash(), identity=identity)  # a name that `jobs` does not have

    async with Running(make(db, queue, [], jobs_lookup=lookup, identity_type=Teacher, runner=runner_for(db))):
        row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "InvalidJobIdentity"
    assert lookup.calls == []  # the application's code was not asked about a message that cannot run
    assert await queue.receive(0.05) is None  # and the message is acknowledged
    assert "secret-value-123" not in caplog.text


async def test_the_worker_caches_nothing(db, queue):
    lookup = Lookup({"shout": Shout})
    first = await submit(db, queue, Shout, "a")
    second = await submit(db, queue, Shout, "b")

    async with Running(make(db, queue, [], jobs_lookup=lookup)):
        await final(db, first)
        await final(db, second)

    assert len(lookup.calls) == 2


# what the worker checks


async def test_a_lookup_that_finds_nothing_is_an_unknown_job_and_the_text_says_a_lookup_was_asked(db, queue, caplog):
    lookup = Lookup()
    job_id = await submit(db, queue, Shout, "hello")

    with caplog.at_level(logging.ERROR, logger="featherweight"):
        async with Running(make(db, queue, [], jobs_lookup=lookup)):
            row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "UnknownJob"
    assert "`jobs_lookup` found nothing" in caplog.text


async def test_without_a_lookup_an_unknown_job_is_as_it_was(db, queue, caplog):
    job_id = await submit(db, queue, Shout, "hello")

    with caplog.at_level(logging.ERROR, logger="featherweight"):
        async with Running(make(db, queue, [])):
            row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "UnknownJob"
    assert "jobs_lookup" not in caplog.text


async def test_a_job_with_another_name_than_the_one_asked_for_is_refused_and_not_run(db, queue):
    job_id = await enqueue(db, queue, "other", Shout.job_hash())

    async with Running(make(db, queue, [], jobs_lookup=Lookup({"other": Shout}))):  # asked for "other", got "shout"
        row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "JobLookupError"
    assert await queue.receive(0.05) is None  # acknowledged: not retried


async def test_a_job_that_changed_since_it_was_submitted_is_refused_and_not_run(db, queue):
    ShoutV2.ran = False
    job_id = await submit(db, queue, Shout, "hello")  # the hash recorded at submit is that of version 1

    async with Running(make(db, queue, [], jobs_lookup=Lookup({"shout": ShoutV2}))):
        row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "JobLookupError"
    assert ShoutV2.ran is False


async def test_a_lookup_that_raises_fails_the_job_and_the_worker_goes_on(db, queue, caplog):
    broken = await enqueue(db, queue, "unlisted", Shout.job_hash(), text="x")
    healthy = await submit(db, queue, Shout, "still here")  # a name `jobs` has: the lookup is not asked

    with caplog.at_level(logging.ERROR, logger="featherweight"):
        async with Running(make(db, queue, [Shout], jobs_lookup=Lookup(raises=ConnectionError("db is away")))):
            broken_row = await final(db, broken)
            healthy_row = await final(db, healthy)

    assert broken_row.status is Status.FAILED and broken_row.error_type == "JobLookupError"
    assert healthy_row.status is Status.SUCCESS
    assert "ConnectionError" in caplog.text and "db is away" in caplog.text  # the application's own error is in the log


async def test_something_that_is_not_a_job_is_refused(db, queue):
    job_id = await enqueue(db, queue, "shout", Shout.job_hash())

    async with Running(make(db, queue, [], jobs_lookup=Lookup({"shout": int}))):
        row = await final(db, job_id)

    assert row.status is Status.FAILED and row.error_type == "JobLookupError"


async def test_a_job_that_was_made_without_a_hash_is_not_checked_against_one(db, queue):
    job_id = await db.create_job("shout")  # not made by a client: there is no hash recorded
    await queue.enqueue(JobMessage(job_id=job_id, job_name="shout", input={"text": "x"}))
    lookup = Lookup({"shout": Shout})

    async with Running(make(db, queue, [], jobs_lookup=lookup)):
        row = await final(db, job_id)

    assert row.status is Status.SUCCESS and lookup.calls == [("shout", None, None)]
