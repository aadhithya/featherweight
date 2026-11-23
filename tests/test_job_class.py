"""The Job class, its context, and an agent as a job. Fake models only."""

import json

import pytest
from fakes import Echo, EchoInput, EchoOutput, Reactive, runner_with, structured_reply
from pydantic import BaseModel

from featherweight.fingerprint import agent_hash, job_hash
from featherweight.jobs import Job, JobContext, RunFailed, as_job
from featherweight.models import Status
from featherweight.runner import AgentRunner
from featherweight.streaming import StreamingAgent


class In(BaseModel):
    text: str


class Out(BaseModel):
    shouted: str


class Shout(Job):
    name = "shout"
    version = "1"
    input_model = In
    output_model = Out

    async def run(self, ctx: JobContext, input: In) -> Out:
        return Out(shouted=input.text.upper())


# the class


def test_a_job_is_a_declared_class_with_a_hash():
    assert (Shout.name, Shout.version, Shout.input_model, Shout.output_model) == ("shout", "1", In, Out)
    assert Shout.job_hash() == job_hash("shout", "1", In, Out)
    assert len(Shout.job_hash()) == 64


def test_a_job_that_has_not_written_run_cannot_be_built():
    class Half(Job):  # still abstract: it is a base, not a job
        pass

    with pytest.raises(TypeError, match="abstract"):
        Half()  # ty: ignore[abstract-class-instantiation]


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("name", "", "name must be"),
        ("name", "1starts-with-a-digit", "name must be"),
        ("name", "has space", "name must be"),
        ("name", 3, "name must be"),
        ("version", "", "version must be"),
        ("version", 1, "version must be"),
        ("input_model", dict, "input_model must be a Pydantic"),
        ("output_model", "Out", "output_model must be a Pydantic"),
    ],
)
def test_a_job_with_a_bad_declaration_is_refused_when_the_class_is_defined(attribute, value, message):
    fields = {"name": "x", "version": "1", "input_model": In, "output_model": Out, attribute: value}

    with pytest.raises(TypeError, match=message):

        class Bad(Job):
            name = fields["name"]
            version = fields["version"]
            input_model = fields["input_model"]
            output_model = fields["output_model"]

            async def run(self, ctx, input):
                return None


def test_a_missing_model_is_refused_when_the_class_is_defined():
    with pytest.raises(TypeError, match="input_model must be a Pydantic"):

        class NoModels(Job):
            name = "x"
            version = "1"

            async def run(self, ctx, input):
                return None


def test_the_hash_changes_with_the_name_the_version_and_either_schema():
    class Other(BaseModel):
        shouted: str
        extra: int = 0

    class Described(BaseModel):
        text: str = "x"

    base = job_hash("shout", "1", In, Out)
    assert job_hash("shout", "1", In, Out) == base
    assert len({base, job_hash("other", "1", In, Out), job_hash("shout", "2", In, Out)}) == 3
    assert job_hash("shout", "1", In, Other) != base
    assert job_hash("shout", "1", Described, Out) != base


# the context


async def test_the_context_runs_agents_in_the_job(db):
    def answer(user_text, call_number):
        return structured_reply("EchoOutput", {"echoed": json.loads(user_text)["text"]})

    runner = runner_with(db, Reactive(respond=answer, delay_s=0), max_attempts=1)
    job_id = await db.create_job("shout")
    ctx = JobContext(runner=runner, job_id=job_id)

    one = await ctx.run(Echo, EchoInput(text="a"))
    many = await ctx.run_many(Echo, [EchoInput(text="b"), EchoInput(text="c")])

    assert one.status is Status.SUCCESS and [r.output.echoed for r in many] == ["b", "c"]
    assert [r.job_id for r in await db.list_attempts()] == [job_id] * 3  # all three belong to the job


async def test_the_context_is_frozen(db):
    ctx = JobContext(runner=AgentRunner(db), job_id="j")
    with pytest.raises(Exception, match="frozen"):
        ctx.job_id = "other"


# an agent as a job


def test_an_agent_can_be_a_job():
    job = as_job(Echo)
    assert issubclass(job, Job) and (job.name, job.version) == ("echo", "0.1.0")
    assert (job.input_model, job.output_model) == (EchoInput, EchoOutput)
    assert job.job_hash() == agent_hash(Echo())
    assert as_job(Echo) is job  # one class per agent
    assert as_job(Shout) is Shout  # a job stays as it is


def test_a_streaming_agent_cannot_be_a_job():
    class Chat(StreamingAgent):
        name: str = "chat"
        description: str = "x"
        version: str = "1"
        prompt: str = "x"

    with pytest.raises(TypeError, match="streams to a person"):
        as_job(Chat)
    with pytest.raises(TypeError, match="neither a Job nor an agent"):
        as_job(int)


async def test_an_agent_job_returns_the_output_and_raises_when_the_agent_fails(db):
    ok = Reactive(respond=lambda text, n: structured_reply("EchoOutput", {"echoed": "hi"}), delay_s=0)
    runner = runner_with(db, ok, max_attempts=1)
    ctx = JobContext(runner=runner, job_id=await db.create_job("echo"))
    assert (await as_job(Echo)().run(ctx, EchoInput(text="a"))).echoed == "hi"

    broken = runner_with(db, Reactive(respond=lambda text, n: RuntimeError("down"), delay_s=0), max_attempts=1)
    with pytest.raises(RunFailed, match="down"):
        await as_job(Echo)().run(JobContext(runner=broken, job_id=await db.create_job("echo")), EchoInput(text="a"))


# the job columns


async def test_the_job_columns_hold_the_hashes_and_the_time_it_started(db):
    job_id = await db.create_job("shout", job_hash=Shout.job_hash(), input_hash="a" * 64)
    row = await db.get_job(job_id)
    assert row is not None and (row.job_hash, row.input_hash) == (Shout.job_hash(), "a" * 64)
    assert row.started_at is None and row.cancel_requested is False
    await db.set_job_status(job_id, Status.RUNNING)
    running = await db.get_job(job_id)
    assert running is not None and running.started_at is not None
    await db.set_job_status(job_id, Status.SUCCESS)
    done = await db.get_job(job_id)
    assert done is not None and done.started_at == running.started_at
