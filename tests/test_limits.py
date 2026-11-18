"""The concurrency limit on AgentRunner (an asyncio.Semaphore around each attempt). LLM is mocked."""

import asyncio

import pytest

from fakes import Echo, EchoInput, Reactive, respond, runner_with
from micro.models import RunStatus
from micro.runner import ENV_MAX_CONCURRENT, AgentRunner, max_concurrent_from_env


async def run_many(runner, texts):
    return await asyncio.gather(*(runner.run(Echo, EchoInput(text=t)) for t in texts))


async def test_never_more_than_the_limit_in_flight_and_everything_completes(db):
    model = Reactive(respond=respond)
    texts = [f"ok-{i}" for i in range(12)]

    results = await run_many(runner_with(db, model, max_concurrent=3), texts)

    assert model.max_in_flight == 3  # reached the limit, never went over it
    assert all(r.status == RunStatus.SUCCESS for r in results)
    assert [r.output.echoed for r in results] == texts


def test_default_limit_is_10():
    assert runner_with(None, Reactive(respond=respond)).max_concurrent == 10


async def test_limit_comes_from_the_environment(db, monkeypatch):
    monkeypatch.setenv(ENV_MAX_CONCURRENT, "2")
    model = Reactive(respond=respond)
    runner = runner_with(db, model)

    await run_many(runner, [f"ok-{i}" for i in range(8)])

    assert runner.max_concurrent == 2
    assert model.max_in_flight == 2


async def test_explicit_argument_beats_the_environment(db, monkeypatch):
    monkeypatch.setenv(ENV_MAX_CONCURRENT, "2")
    assert runner_with(db, Reactive(respond=respond), max_concurrent=5).max_concurrent == 5


def test_blank_environment_value_means_default(monkeypatch):
    monkeypatch.setenv(ENV_MAX_CONCURRENT, "  ")
    assert max_concurrent_from_env() == 10


@pytest.mark.parametrize("value", ["abc", "1.5", "0", "-3"])
def test_invalid_environment_value_is_rejected(monkeypatch, value):
    monkeypatch.setenv(ENV_MAX_CONCURRENT, value)
    with pytest.raises(ValueError, match=ENV_MAX_CONCURRENT):
        max_concurrent_from_env()
    with pytest.raises(ValueError):
        AgentRunner(None)


async def test_a_waiting_attempt_is_INIT_until_it_gets_a_slot(db):
    model = Reactive(respond=respond, delay_s=0.2)
    runner = runner_with(db, model, max_concurrent=1)

    first = asyncio.create_task(runner.run(Echo, EchoInput(text="ok-1")))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(runner.run(Echo, EchoInput(text="ok-2")))
    await asyncio.sleep(0.1)

    rows = await db.list_runs()
    assert [(r.input["text"], r.status) for r in rows] == [
        ("ok-1", RunStatus.RUNNING),
        ("ok-2", RunStatus.INIT),  # queued, not running
    ]

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
