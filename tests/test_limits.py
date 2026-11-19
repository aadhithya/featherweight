"""The concurrency limit on AgentRunner (an asyncio.Semaphore around each attempt). LLM is mocked."""

import asyncio

import pytest
from fakes import Echo, EchoInput, Reactive, respond, runner_with

from featherweight.fingerprint import input_hash
from featherweight.models import RunStatus
from featherweight.runner import ENV_MAX_CONCURRENT, AgentRunner


async def run_many(runner, texts):
    return await asyncio.gather(*(runner.run(Echo, EchoInput(text=t)) for t in texts))


async def test_never_more_than_the_limit_in_flight_and_everything_completes(db):
    model = Reactive(respond=respond)
    texts = [f"ok-{i}" for i in range(12)]

    results = await run_many(runner_with(db, model, max_concurrent=3), texts)

    assert model.max_in_flight == 3  # reached the limit, never went over it
    assert all(r.status == RunStatus.SUCCESS for r in results)
    assert [r.output.echoed for r in results] == texts


async def test_limit_comes_from_the_environment(db, monkeypatch):
    monkeypatch.setenv(ENV_MAX_CONCURRENT, "2")
    model = Reactive(respond=respond)
    runner = runner_with(db, model)

    await run_many(runner, [f"ok-{i}" for i in range(8)])

    assert runner.max_concurrent == 2
    assert model.max_in_flight == 2


async def test_a_waiting_attempt_is_INIT_until_it_gets_a_slot(db):
    model = Reactive(respond=respond, delay_s=0.2)
    runner = runner_with(db, model, max_concurrent=1)

    first = asyncio.create_task(runner.run(Echo, EchoInput(text="ok-1")))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(runner.run(Echo, EchoInput(text="ok-2")))
    await asyncio.sleep(0.1)

    rows = await db.list_runs()
    assert [(r.input_hash, r.status) for r in rows] == [
        (input_hash(EchoInput(text="ok-1")), RunStatus.RUNNING),
        (input_hash(EchoInput(text="ok-2")), RunStatus.INIT),  # queued, not running
    ]
    assert rows[0].started_at is not None and rows[1].started_at is None  # it has not held a slot yet

    results = await asyncio.gather(first, second)
    assert all(r.status == RunStatus.SUCCESS for r in results)
    assert model.max_in_flight == 1


async def test_a_retry_delay_does_not_hold_a_slot(db):
    model = Reactive(respond=respond, delay_s=0.01)
    runner = runner_with(db, model, max_concurrent=1, retry_delay_s=0.3)
    finished = []

    async def go(text):
        result = await runner.run(Echo, EchoInput(text=text))
        finished.append(text)
        return result

    flaky = asyncio.create_task(go("flaky-1"))
    await asyncio.sleep(0.1)  # flaky-1's first attempt has failed and it is now in its retry delay
    other = await go("ok-1")  # must get the only slot right away

    assert finished == ["ok-1"]  # done before flaky-1's retry
    assert other.status == RunStatus.SUCCESS
    assert (await flaky).status == RunStatus.SUCCESS
    assert finished == ["ok-1", "flaky-1"]


# fan-out: AgentRunner.run_many


async def test_run_many_returns_results_in_input_order_and_respects_the_limit(db):
    model = Reactive(respond=respond)
    texts = [f"ok-{i}" for i in range(9)]

    results = await runner_with(db, model, max_concurrent=3).run_many(Echo, [EchoInput(text=t) for t in texts])

    assert [r.output.echoed for r in results] == texts
    assert model.max_in_flight == 3  # no limit of its own: the runner's semaphore bounds it


async def test_run_many_returns_failures_as_results_and_handles_the_job_id_and_no_inputs(db):
    job_id = await db.create_job("learn")
    runner = runner_with(db, Reactive(respond=respond))
    inputs = [EchoInput(text=t) for t in ("ok-1", "fail-1", "flaky-1")]

    assert await runner.run_many(Echo, [], job_id=job_id) == []
    results = await runner.run_many(Echo, inputs, job_id=job_id)

    assert [r.status for r in results] == [RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.SUCCESS]
    assert results[1].error == "RuntimeError: down fail-1"
    assert results[2].attempts == 2
    assert len(await db.list_runs(job_id=job_id)) == 1 + 3 + 2  # ok-1, fail-1 x3, flaky-1 x2


LIMITS = [  # the argument, its environment variable and its default
    ("max_concurrent", "MAX_CONCURRENT_AGENT_RUNS", 10),
    ("max_depth", "MAX_AGENT_DEPTH", 3),
    ("max_launches", "MAX_AGENT_LAUNCHES", 10),
]


@pytest.mark.parametrize("arg,var,default", LIMITS)
def test_a_limit_has_a_default_comes_from_the_environment_and_the_argument_wins(monkeypatch, arg, var, default):
    assert getattr(AgentRunner(None), arg) == default
    monkeypatch.setenv(var, "4")
    assert getattr(AgentRunner(None), arg) == 4
    assert getattr(AgentRunner(None, **{arg: 2}), arg) == 2  # the argument wins over the variable
    monkeypatch.setenv(var, "  ")
    assert getattr(AgentRunner(None), arg) == default  # a blank value means the default


@pytest.mark.parametrize("arg,var,default", LIMITS)
@pytest.mark.parametrize(
    "value,message",
    [("0", "at least 1"), ("-3", "at least 1"), ("many", "must be an integer"), ("1.5", "must be an integer")],
)
def test_a_bad_limit_in_the_environment_is_refused_and_names_the_variable(
    monkeypatch, arg, var, default, value, message
):
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=message) as error:
        AgentRunner(None)
    assert var in str(error.value)


@pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"max_concurrent": 0}, {"max_depth": 0}, {"max_launches": 0}])
def test_a_limit_argument_below_one_is_refused(kwargs):
    with pytest.raises(ValueError, match="at least 1"):
        AgentRunner(None, **kwargs)
