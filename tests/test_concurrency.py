"""Many runs at once against one Database and one AgentRunner. LLM is mocked."""

import asyncio

from fakes import Echo, EchoInput, Reactive, respond, runner_with
from micro.models import RunStatus
from micro.store import RunStore


async def test_many_concurrent_runs_do_not_interfere(db):
    model = Reactive(respond=respond)
    runner = runner_with(db, model)
    job_id = await db.create_job("learn")
    texts = [f"ok-{i}" for i in range(25)]

    results = await asyncio.gather(
        *(runner.run(Echo, EchoInput(text=t), job_id=job_id) for t in texts)
    )

    assert model.max_in_flight > 1  # the runs really overlapped
    assert all(r.status == RunStatus.SUCCESS for r in results)
    assert [r.output.echoed for r in results] == texts  # each result belongs to its own input
    assert len({r.agent_run_id for r in results}) == 25

    rows = await db.list_runs(job_id=job_id)
    assert len(rows) == 25
    assert len({r.invocation_id for r in rows}) == 25
    for row in rows:
        assert row.status == RunStatus.SUCCESS
        assert row.output["echoed"] == row.input["text"]  # no crosstalk between rows
        assert row.input_tokens == 5 and row.output_tokens == 7
        assert row.completed_at is not None


async def test_concurrent_store_access_stays_scoped_per_job():
    store = RunStore()
    jobs = [f"job-{i}" for i in range(40)]

    await asyncio.gather(*(store.set(job, "state", {"job": job}) for job in jobs))
    values = await asyncio.gather(*(store.get(job, "state") for job in jobs))

    assert values == [{"job": job} for job in jobs]
    await store.close()
