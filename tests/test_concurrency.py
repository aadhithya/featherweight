"""Many runs at once against one Database and one AgentRunner. LLM is mocked."""

import asyncio

from fakes import Echo, EchoInput, Reactive, respond, runner_with

from featherweight.fingerprint import input_hash, output_hash
from featherweight.models import RunStatus


async def test_many_concurrent_runs_do_not_interfere(db):
    model = Reactive(respond=respond)
    runner = runner_with(db, model)
    job_id = await db.create_job("learn")
    texts = [f"ok-{i}" for i in range(25)]

    results = await asyncio.gather(*(runner.run(Echo, EchoInput(text=t), job_id=job_id) for t in texts))

    assert model.max_in_flight > 1  # the runs really overlapped
    assert all(r.status == RunStatus.SUCCESS for r in results)
    assert [r.output.echoed for r in results] == texts  # each result belongs to its own input
    assert len({r.agent_run_id for r in results}) == 25

    rows = await db.list_runs(job_id=job_id)
    assert len(rows) == 25
    assert len({r.invocation_id for r in rows}) == 25
    by_run = {r.agent_run_id: r for r in results}
    for row in rows:
        assert row.status == RunStatus.SUCCESS
        result = by_run[row.agent_run_id]
        assert row.input_hash == input_hash(EchoInput(text=result.output.echoed))  # no crosstalk between rows
        assert row.output_hash == output_hash(result.output)
        assert row.input_tokens == 5 and row.output_tokens == 7
        assert row.started_at is not None and row.completed_at is not None
