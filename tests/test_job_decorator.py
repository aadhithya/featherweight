"""@job: a Job class from an async function. Fake models only."""

import json

import pytest
from fakes import Echo, EchoInput, EchoOutput, Reactive, runner_with, structured_reply
from pydantic import BaseModel

from featherweight.jobs import Job, JobContext, job


class In(BaseModel):
    text: str


class Out(BaseModel):
    shouted: str


def test_a_bare_decorator_makes_a_job_class_from_the_function():
    @job
    async def shout(ctx: JobContext, input: In) -> Out:
        """Shouts."""
        return Out(shouted=input.text.upper())

    assert isinstance(shout, type) and issubclass(shout, Job)
    assert (shout.name, shout.version, shout.input_model, shout.output_model) == ("shout", "1", In, Out)
    assert shout.__doc__ == "Shouts." and shout.__name__ == "shout"
    assert shout.job_hash() == job.__wrapped__(shout.function).job_hash() if hasattr(job, "__wrapped__") else True


def test_the_name_and_the_version_can_be_given():
    @job(name="shout.v2", version="2")
    async def anything(ctx: JobContext, input: In) -> Out:
        return Out(shouted="")

    assert (anything.name, anything.version) == ("shout.v2", "2")

    @job()
    async def plain(ctx: JobContext, input: In) -> Out:
        return Out(shouted="")

    assert (plain.name, plain.version) == ("plain", "1")


async def test_the_class_runs_the_function_with_the_context(db):
    def answer(user_text, call_number):
        return structured_reply("EchoOutput", {"echoed": json.loads(user_text)["text"]})

    @job
    async def shout(ctx: JobContext, input: In) -> Out:
        result = await ctx.run(Echo, EchoInput(text=input.text))
        assert isinstance(result.output, EchoOutput)
        return Out(shouted=result.output.echoed.upper())

    runner = runner_with(db, Reactive(respond=answer, delay_s=0), max_attempts=1)
    ctx = JobContext(runner=runner, job_id=await db.create_job("shout"))
    assert await shout().run(ctx, In(text="hi")) == Out(shouted="HI")
    assert await shout.function(ctx, In(text="yo")) == Out(shouted="YO")  # the function is still there
    assert len(await db.list_attempts(job_id=ctx.job_id)) == 2


def test_the_hash_follows_the_declaration():
    @job
    async def a(ctx: JobContext, input: In) -> Out:
        return Out(shouted="")

    @job(version="2")
    async def b(ctx: JobContext, input: In) -> Out:
        return Out(shouted="")

    assert a.job_hash() != b.job_hash()


@pytest.mark.parametrize("how", ["sync", "one argument", "three arguments"])
def test_a_function_of_the_wrong_shape_is_refused_when_decorated(how):
    def sync(ctx: JobContext, input: In) -> Out:
        raise NotImplementedError

    async def one(ctx: JobContext) -> Out:
        raise NotImplementedError

    async def three(ctx: JobContext, input: In, extra: int) -> Out:
        raise NotImplementedError

    with pytest.raises(TypeError, match="@job needs"):
        job({"sync": sync, "one argument": one, "three arguments": three}[how])


def test_missing_or_wrong_annotations_are_refused_when_decorated():
    async def no_input(ctx, input) -> Out:
        raise NotImplementedError

    async def no_return(ctx, input: In):
        raise NotImplementedError

    async def not_models(ctx, input: dict) -> str:
        raise NotImplementedError

    for function in (no_input, no_return, not_models):
        with pytest.raises(TypeError, match="Pydantic model"):
            job(function)


def test_it_is_on_the_package():
    assert job is job and Job is Job
