"""Resuming a job: a step that the old job already finished is answered from its kept result. Fake models only."""

import json
from typing import ClassVar

import pytest
from fakes import (
    Echo,
    EchoInput,
    EchoOutput,
    FakeSettings,
    Reactive,
    Scripted,
    run_pipeline,
    runner_with,
    structured_reply,
    tool_reply,
)
from test_subagents import Parent, ParentInput

from featherweight.db import RunEnd, RunStart
from featherweight.fingerprint import agent_hash, input_hash, output_hash, text_hash
from featherweight.model_settings import ModelSettings
from featherweight.models import RunStatus
from featherweight.results import ResultKey, SqliteResultStore


def model(broken: set[str] | None = None, echoed: str | None = None) -> Reactive:
    """Echoes the input text (or answers `echoed`); fails for a text in `broken`. `texts(model)` says which inputs
    reached it."""
    broken = broken or set()

    def answer(user_text, call_number):
        text = json.loads(user_text)["text"]
        if text in broken:
            return RuntimeError(f"down {text}")
        return structured_reply("EchoOutput", {"echoed": echoed or text})

    return Reactive(respond=answer, delay_s=0)


def texts(fake: Reactive) -> list[str]:
    return sorted(json.loads(t)["text"] for t in fake.counts)


def steps(runner, inputs, agent=Echo):
    """A pipeline that runs one agent per text, in order, and stops at the first failure."""

    async def pipeline(job_id):
        for text in inputs:
            result = await runner.run(agent, EchoInput(text=text), job_id=job_id)
            if result.status == RunStatus.FAILED:
                raise RuntimeError(result.error)

    return pipeline


async def run_steps(db, inputs, fake=None, agent=Echo, **kwargs):
    kwargs.setdefault("resumable", True)  # a job that is to be resumed keeps its results: decided before it runs
    return await run_pipeline(db, "p", steps(runner_with(db, fake or model(), max_attempts=1), inputs, agent), **kwargs)


async def test_a_resumed_job_reuses_step_one_and_runs_the_steps_after_it(db):
    failing = model({"b"})
    failed = await run_steps(db, "abc", failing)
    assert failed.status == RunStatus.FAILED and texts(failing) == ["a", "b"]

    healthy = model()
    outcome = await run_steps(db, "abc", healthy, resume_from=failed.job_id)

    assert outcome.status == RunStatus.SUCCESS
    assert texts(healthy) == ["b", "c"]  # "a" never reached the model
    old = await db.list_runs(job_id=failed.job_id)
    new = await db.list_runs(job_id=outcome.job_id)
    assert [r.status for r in new] == [RunStatus.SUCCESS] * 3
    assert new[0].reused_from_agent_run_id == old[0].agent_run_id and new[0].agent_run_id != old[0].agent_run_id
    assert (new[0].input_tokens, new[0].output_tokens) == (0, 0)
    assert new[0].output_hash == old[0].output_hash == output_hash(EchoOutput(echoed="a"))
    assert [r.reused_from_agent_run_id for r in new[1:]] == [None, None]
    assert new[1].input_tokens == 5  # the steps that ran were counted


async def test_the_new_job_records_resumed_from_and_the_old_job_is_unchanged(db):
    failed = await run_steps(db, "ab", model({"b"}))
    before = await db.get_job(failed.job_id)
    before_runs = await db.list_runs(job_id=failed.job_id)

    outcome = await run_steps(db, "ab", resume_from=failed.job_id)

    assert (await db.get_job(outcome.job_id)).resumed_from == failed.job_id
    assert before.resumed_from is None and await db.get_job(failed.job_id) == before
    assert await db.list_runs(job_id=failed.job_id) == before_runs


async def test_a_reused_result_says_so_and_calls_no_model(db):
    first = await run_steps(db, "a")
    scripted = Scripted(script=[])  # any call to the model would fail
    runner = runner_with(db, scripted, max_attempts=1)
    job_id = await db.create_job("p", resume_from=first.job_id)

    result = await runner.run(Echo, EchoInput(text="a"), job_id=job_id)

    assert result.status == RunStatus.SUCCESS and result.reused is True
    assert result.output == EchoOutput(echoed="a") and (result.input_tokens, result.output_tokens) == (0, 0)
    assert scripted.calls == 0
    assert (await db.get_run(result.agent_run_id)).status == RunStatus.SUCCESS


async def test_a_reused_row_adds_nothing_to_the_job_totals(db):
    first = await run_steps(db, "a")
    outcome = await run_steps(db, "a", resume_from=first.job_id)

    async with db.conn.execute(
        "SELECT SUM(input_tokens), SUM(output_tokens) FROM agent_runs WHERE job_id = ?", (outcome.job_id,)
    ) as cur:
        assert tuple(await cur.fetchone()) == (0, 0)


class Edited(Echo):
    prompt: str = "A different prompt."


class Bumped(Echo):
    version: str = "0.2.0"


class Retuned(Echo):
    model: ModelSettings = FakeSettings(model="other-model")


@pytest.mark.parametrize("agent", [Edited, Bumped, Retuned])
async def test_a_changed_prompt_version_or_model_setting_reruns_the_step(db, agent):
    first = await run_steps(db, "a")
    counting = model()

    outcome = await run_steps(db, "a", counting, agent, resume_from=first.job_id)

    assert outcome.status == RunStatus.SUCCESS and texts(counting) == ["a"]
    (row,) = await db.list_runs(job_id=outcome.job_id)
    assert row.reused_from_agent_run_id is None


async def test_a_changed_input_reruns_the_step_and_an_unchanged_one_is_reused(db):
    first = await run_steps(db, "ab")
    counting = model()

    await run_steps(db, ["a", "changed"], counting, resume_from=first.job_id)

    assert texts(counting) == ["changed"]


async def test_a_step_is_found_by_what_it_was_given_not_by_its_place(db):
    first = await run_steps(db, "ab")
    counting = model()

    await run_steps(db, ["b", "x"], counting, resume_from=first.job_id)

    assert texts(counting) == ["x"]


class Strict(Echo):
    """`Echo` with a rule on the output, which a stored output can come to fail."""

    reject: ClassVar[set[str]] = set()

    def check_output(self, agent_input, output):
        if output.echoed in self.reject:
            raise ValueError("not allowed any more")


async def test_an_output_that_check_output_now_rejects_runs_the_agent(db, monkeypatch):
    first = await run_steps(db, "a", agent=Strict)
    monkeypatch.setattr(Strict, "reject", {"a"})
    counting = model(echoed="fresh")

    outcome = await run_steps(db, "a", counting, Strict, resume_from=first.job_id)

    assert outcome.status == RunStatus.SUCCESS and texts(counting) == ["a"]
    (row,) = await db.list_runs(job_id=outcome.job_id)
    assert row.reused_from_agent_run_id is None and row.output_hash == output_hash(EchoOutput(echoed="fresh"))


async def test_an_output_that_no_longer_validates_runs_the_agent(db):
    first = await run_steps(db, "a")
    key = ResultKey(job_id=first.job_id, agent_hash=agent_hash(Echo()), input_hash=input_hash(EchoInput(text="a")))
    await SqliteResultStore(db).put(key, {"echoed": 5})  # the kept result no longer fits the output model
    counting = model()

    outcome = await run_steps(db, "a", counting, resume_from=first.job_id)

    assert outcome.status == RunStatus.SUCCESS and texts(counting) == ["a"]
    (row,) = await db.list_runs(job_id=outcome.job_id)
    assert row.reused_from_agent_run_id is None and row.output_hash == output_hash(EchoOutput(echoed="a"))


async def old_row(db, job_id, status):
    """A run of `Echo` on the text "a", as an earlier job would have left it, with its result kept."""
    input_ = EchoInput(text="a")
    run_id = await db.create_run(
        RunStart(
            agent_run_id=f"{job_id}-{status}",
            invocation_id="inv",
            attempt=1,
            job_id=job_id,
            agent_name="echo",
            agent_version="0.1.0",
            agent_hash=agent_hash(Echo()),
            system_prompt_hash=text_hash("prompt"),
            tools_hash=text_hash("tools"),
            provider="fake",
            model_name="fake-model",
            reasoning="-",
            settings={"model": "fake-model"},
            input_hash=input_hash(input_),
        )
    )
    if status == "RUNNING":
        await db.mark_run_running(run_id)
    elif status in ("SUCCESS", "FAILED"):
        await db.finish_run(run_id, RunEnd(status=RunStatus(status), output_hash=text_hash("a")))
    key = ResultKey(job_id=job_id, agent_hash=agent_hash(Echo()), input_hash=input_hash(input_))
    await SqliteResultStore(db).put(key, {"echoed": "a"})


@pytest.mark.parametrize("status", ["FAILED", "INIT", "RUNNING"])
async def test_a_row_that_did_not_succeed_is_never_reused(db, status):
    old = await db.create_job("p", resumable=True)
    await old_row(db, old, status)
    counting = model()

    outcome = await run_steps(db, "a", counting, resume_from=old)

    assert texts(counting) == ["a"]
    (row,) = await db.list_runs(job_id=outcome.job_id)
    assert row.reused_from_agent_run_id is None


async def test_a_helper_row_is_reused_when_it_succeeded(db):
    old = await db.create_job("p", resumable=True)
    await old_row(db, old, "SUCCESS")
    counting = model()

    await run_steps(db, "a", counting, resume_from=old)

    assert texts(counting) == []


async def test_run_many_with_one_failed_input_reruns_only_that_input(db):
    async def fan_out(runner, job_id):
        return await runner.run_many(Echo, [EchoInput(text=t) for t in "abc"], job_id=job_id)

    runner = runner_with(db, model({"b"}), max_attempts=1)
    failed = await run_pipeline(db, "p", lambda job_id: fan_out(runner, job_id), resumable=True)
    assert [r.status for r in failed.result or []] == [RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.SUCCESS]

    healthy = model()
    runner = runner_with(db, healthy, max_attempts=1)
    resumed = await run_pipeline(db, "p", lambda job_id: fan_out(runner, job_id), resume_from=failed.job_id)

    assert texts(healthy) == ["b"]
    assert [r.reused for r in resumed.result or []] == [True, False, True]
    assert all(r.status == RunStatus.SUCCESS for r in resumed.result or [])


async def test_identical_inputs_in_one_run_many_are_all_reused(db):
    async def fan_out(runner, job_id):
        return await runner.run_many(Echo, [EchoInput(text="a")] * 2, job_id=job_id)

    runner = runner_with(db, model(), max_attempts=1)
    first = await run_pipeline(db, "p", lambda job_id: fan_out(runner, job_id), resumable=True)
    resumed = await run_pipeline(db, "p", lambda job_id: fan_out(runner, job_id), resume_from=first.job_id)

    assert [r.reused for r in resumed.result or []] == [True, True]


def parent_script(topic):
    launch = tool_reply("launch_agent", {"agent_name": "child", "input_json": json.dumps({"topic": topic})}, "c1")
    return [
        launch,
        structured_reply("ChildOutput", {"summary": f"about {topic}"}),
        structured_reply("ParentOutput", {"answer": "done"}),
    ]


async def test_a_succeeded_parent_is_reused_whole_and_its_children_never_launch(db):
    runner = runner_with(db, Scripted(script=parent_script("x")), max_attempts=1)
    old = await db.create_job("p", resumable=True)
    first = await runner.run(Parent, ParentInput(task="go"), job_id=old)
    assert first.status == RunStatus.SUCCESS and len(await db.list_runs(job_id=old)) == 2  # the parent and its child

    scripted = Scripted(script=[])
    runner = runner_with(db, scripted, max_attempts=1)
    new = await db.create_job("p", resume_from=old)
    second = await runner.run(Parent, ParentInput(task="go"), job_id=new)

    assert second.reused and second.output == first.output and scripted.calls == 0
    (row,) = await db.list_runs(job_id=new)  # no row for the child
    assert row.depth == 1 and row.reused_from_agent_run_id == first.agent_run_id


async def test_a_failed_parent_reruns_and_its_child_is_not_looked_up(db):
    runner = runner_with(db, Scripted(script=parent_script("x")), max_attempts=1)
    old = await db.create_job("p", resumable=True)
    await runner.run(Parent, ParentInput(task="go"), job_id=old)
    # Only the child (depth 2) is left as a SUCCESS row of the old job.
    await db.conn.execute("UPDATE agent_runs SET status = 'FAILED' WHERE job_id = ? AND depth = 1", (old,))
    await db.conn.commit()

    scripted = Scripted(script=parent_script("x"))
    runner = runner_with(db, scripted, max_attempts=1)
    new = await db.create_job("p", resume_from=old)
    result = await runner.run(Parent, ParentInput(task="go"), job_id=new)

    assert result.status == RunStatus.SUCCESS and not result.reused
    runs = await db.list_runs(job_id=new)
    assert [(r.depth, r.reused_from_agent_run_id) for r in runs] == [(1, None), (2, None)]  # the child ran again
    assert scripted.calls == 3


async def test_a_run_with_no_job_or_a_job_that_resumes_nothing_is_never_reused(db):
    first = await run_steps(db, "a")
    runner = runner_with(db, model(), max_attempts=1)

    assert (await runner.run(Echo, EchoInput(text="a"))).reused is False
    assert (await runner.run(Echo, EchoInput(text="a"), job_id=first.job_id)).reused is False


async def test_resuming_an_unknown_job_is_refused(db):
    with pytest.raises(LookupError, match="nope"):
        await db.create_job("p", resume_from="nope")


async def test_resume_from_and_job_id_cannot_be_combined(db):
    old = await db.create_job("p", resumable=True)
    new = await db.create_job("p")

    async def pipeline(job_id):
        return None

    with pytest.raises(ValueError, match="resume_from"):
        await run_pipeline(db, "p", pipeline, job_id=new, resume_from=old)
