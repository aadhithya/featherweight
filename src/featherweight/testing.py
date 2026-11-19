"""Helpers for testing code that uses Featherweight, and for checking an implementation of one of its abstract classes."""

import asyncio
import copy
import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from featherweight.db import Database, RunEnd, RunStart
from featherweight.models import RunStatus
from featherweight.results import ResultKey, ResultStore


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class MemoryResultStore(ResultStore):
    """A `ResultStore` in a dict: nothing survives the process. For tests, and the smallest example of the class."""

    def __init__(self) -> None:
        self._results: dict[ResultKey, dict[str, Any]] = {}

    async def put(self, key: ResultKey, output: dict[str, Any]) -> None:
        self._results[key] = copy.deepcopy(output)

    async def get(self, key: ResultKey) -> dict[str, Any] | None:
        found = self._results.get(key)
        return None if found is None else copy.deepcopy(found)

    async def delete_job(self, job_id: str) -> None:
        self._results = {key: value for key, value in self._results.items() if key.job_id != job_id}


async def check_result_store(make_store: Callable[[], ResultStore | Awaitable[ResultStore]]) -> None:
    """Run the `ResultStore` contract against a store. `make_store` returns a fresh, empty store (it may be async). Raises
    `AssertionError` that says which rule a store breaks.

        async def test_my_store():
            await check_result_store(lambda: MyStore(...))
    """

    async def fresh() -> ResultStore:
        made = make_store()
        return await made if isinstance(made, Awaitable) else made

    def key(job: str = "job-1", agent: str = "agent-a", input: str = "input-a") -> ResultKey:
        return ResultKey(job_id=job, agent_hash=_hash(agent), input_hash=_hash(input))

    output = {"summary": "Anna met Tom", "n": 3, "ok": True, "none": None, "list": [1, 2.5, "x"], "nested": {"é": "ü"}}

    store = await fresh()
    assert await store.get(key()) is None, "get of a key that was never put must be None"

    await store.put(key(), output)
    assert await store.get(key()) == output, "get must return what put stored, equal as JSON"
    got = await store.get(key())
    assert got is not None
    got["summary"] = "changed by the caller"
    assert await store.get(key()) == output, "changing what get returned must not change the store"

    await store.put(key(), {"summary": "replaced"})
    assert await store.get(key()) == {"summary": "replaced"}, "put on an existing key must replace it"

    for other in (key(job="job-2"), key(agent="agent-b"), key(input="input-b")):
        assert await store.get(other) is None, f"a key that differs in one part must not match: {other}"

    store = await fresh()
    await store.put(key("job-1", "a", "x"), {"v": 1})
    await store.put(key("job-1", "b", "y"), {"v": 2})
    await store.put(key("job-2", "a", "x"), {"v": 3})
    await store.delete_job("job-1")
    assert await store.get(key("job-1", "a", "x")) is None, "delete_job must remove every result of the job"
    assert await store.get(key("job-1", "b", "y")) is None, "delete_job must remove every result of the job"
    assert await store.get(key("job-2", "a", "x")) == {"v": 3}, "delete_job must leave other jobs alone"
    await store.delete_job("job-1")
    await store.delete_job("no-such-job")  # no result: must not fail

    store = await fresh()
    keys = [key(input=f"input-{i}") for i in range(40)]
    await asyncio.gather(*(store.put(k, {"i": i}) for i, k in enumerate(keys)))
    results = await asyncio.gather(*(store.get(k) for k in keys))
    assert results == [{"i": i} for i in range(40)], "concurrent puts and gets must not lose or mix results"


def _run(job_id: str | None, agent_run_id: str, **changes: Any) -> RunStart:
    fields: dict[str, Any] = {
        "agent_run_id": agent_run_id,
        "invocation_id": f"inv-{agent_run_id}",
        "attempt": 1,
        "job_id": job_id,
        "agent_name": "echo",
        "agent_version": "1",
        "agent_hash": _hash("agent"),
        "system_prompt_hash": _hash("prompt"),
        "tools_hash": _hash("tools"),
        "provider": "fake",
        "model_name": "fake-model",
        "reasoning": "low",
        "settings": {"model": "fake-model", "nested": {"a": [1, 2]}},
        "input_hash": _hash("input"),
    }
    return RunStart(**{**fields, **changes})


async def check_database(make_db: Callable[[], Database]) -> None:
    """Run the `Database` contract against an implementation. `make_db` returns a fresh, empty, **unopened** database; the kit
    opens and closes it. Raises `AssertionError` that says which rule an implementation breaks.

        async def test_my_database():
            await check_database(lambda: MyDatabase(...))
    """
    async with make_db() as db:
        # jobs
        assert await db.get_job("no-such-job") is None, "get_job of a job that does not exist must be None"
        job_id = await db.create_job("p")
        job = await db.get_job(job_id)
        assert job is not None and job.name == "p" and job.status is RunStatus.INIT, "a new job is INIT"
        assert job.resumable is False and job.resumed_from is None and job.completed_at is None
        assert job.created_at.tzinfo is not None, "timestamps must be timezone-aware"
        assert await db.create_job("p", job_id="chosen") == "chosen", "create_job must use the id it is given"

        await db.set_job_status(job_id, RunStatus.RUNNING)
        running = await db.get_job(job_id)
        assert running is not None and running.status is RunStatus.RUNNING and running.completed_at is None
        await db.set_job_status(job_id, RunStatus.FAILED, error_type="ValueError", error_hash=_hash("boom"))
        failed = await db.get_job(job_id)
        assert failed is not None and failed.status is RunStatus.FAILED and failed.completed_at is not None
        assert (failed.error_type, failed.error_hash) == ("ValueError", _hash("boom"))
        await db.set_job_status(job_id, RunStatus.SUCCESS, error_type="Ignored", error_hash=_hash("x"))
        done = await db.get_job(job_id)
        assert done is not None and done.error_type is None, "the error is kept only for FAILED"
        try:
            await db.set_job_status("no-such-job", RunStatus.RUNNING)
        except LookupError:
            pass
        else:
            raise AssertionError("set_job_status of a job that does not exist must raise LookupError")

        # resuming: the rules are in `Database.create_job`, so this checks that the implementation stores what it is given
        keeps = await db.create_job("p", resumable=True)
        retry = await db.create_job("p", resume_from=keeps)
        retried = await db.get_job(retry)
        assert retried is not None and retried.resumed_from == keeps and retried.resumable is True
        for bad, error in ((job_id, ValueError), ("no-such-job", LookupError)):
            try:
                await db.create_job("p", resume_from=bad)
            except error:
                pass
            else:
                raise AssertionError(f"resuming {bad!r} must raise {error.__name__}")

        # runs
        assert await db.get_run("no-such-run") is None, "get_run of a run that does not exist must be None"
        first = _run(keeps, "run-1")
        assert await db.create_run(first) == "run-1"
        row = await db.get_run("run-1")
        assert row is not None and row.status is RunStatus.INIT and row.started_at is None and row.completed_at is None
        assert row.settings == first.settings, "settings must come back as the dict that went in"
        assert row.created_at.tzinfo is not None and row.attempt == 1 and row.depth == 1 and row.job_id == keeps
        assert (row.agent_hash, row.system_prompt_hash, row.tools_hash) == (
            first.agent_hash,
            first.system_prompt_hash,
            first.tools_hash,
        )
        await db.mark_run_running("run-1")
        row = await db.get_run("run-1")
        assert row is not None and row.status is RunStatus.RUNNING and row.started_at is not None
        assert row.started_at >= row.created_at - timedelta(seconds=1)
        await db.finish_run(
            "run-1",
            RunEnd(
                status=RunStatus.SUCCESS,
                output_hash=_hash("out"),
                input_tokens=5,
                output_tokens=7,
                latency_ms=12.5,
                reported_model_name="reported",
            ),
        )
        row = await db.get_run("run-1")
        assert row is not None and row.status is RunStatus.SUCCESS and row.completed_at is not None
        assert (row.input_tokens, row.output_tokens, row.latency_ms) == (5, 7, 12.5)
        assert (row.output_hash, row.reported_model_name) == (_hash("out"), "reported")
        try:
            await db.finish_run("no-such-run", RunEnd(status=RunStatus.FAILED))
        except LookupError:
            pass
        else:
            raise AssertionError("finish_run of a run that does not exist must raise LookupError")
        try:
            await db.create_run(_run("no-such-job", "run-x"))
        except Exception:
            assert await db.get_run("run-x") is None, "a run of a job that does not exist must not be written"
        else:
            raise AssertionError("create_run with a job that does not exist must raise")

        # the launch tree, and the order
        await db.create_run(_run(keeps, "run-2", invocation_id="inv-2", parent_agent_run_id="run-1", depth=2))
        await db.create_run(_run(keeps, "run-3", invocation_id="inv-2", attempt=2, thread_id="t-1"))
        await db.create_run(_run(None, "run-4"))
        await db.finish_run(
            "run-3", RunEnd(status=RunStatus.FAILED, error_type="RuntimeError", error_hash=_hash("no"), latency_ms=1.0)
        )
        everything = await db.list_runs()
        assert [r.agent_run_id for r in everything] == ["run-1", "run-2", "run-3", "run-4"], (
            "rows come in the order made"
        )
        assert [r.seq for r in everything] == sorted(r.seq for r in everything)
        assert [r.agent_run_id for r in await db.list_runs(job_id=keeps)] == ["run-1", "run-2", "run-3"]
        assert [r.agent_run_id for r in await db.list_runs(invocation_id="inv-2")] == ["run-2", "run-3"]
        assert [r.agent_run_id for r in await db.list_runs(job_id=keeps, invocation_id="inv-2")] == ["run-2", "run-3"]
        third = await db.get_run("run-3")
        assert third is not None and (third.error_type, third.error_hash) == ("RuntimeError", _hash("no"))
        assert third.thread_id == "t-1" and third.attempt == 2
        second = await db.get_run("run-2")
        assert second is not None and (second.parent_agent_run_id, second.depth) == ("run-1", 2)

        # finding a step to reuse: the first successful top-level run of the job with this agent and this input
        found = await db.find_reusable_run(keeps, _hash("agent"), _hash("input"))
        assert found is not None and found.agent_run_id == "run-1"
        assert await db.find_reusable_run(keeps, _hash("other agent"), _hash("input")) is None
        assert await db.find_reusable_run(keeps, _hash("agent"), _hash("other input")) is None
        assert await db.find_reusable_run(job_id, _hash("agent"), _hash("input")) is None, (
            "another job's runs do not match"
        )
        await db.finish_run("run-2", RunEnd(status=RunStatus.SUCCESS))
        found = await db.find_reusable_run(keeps, _hash("agent"), _hash("input"))
        assert found is not None and found.agent_run_id == "run-1", (
            "a run launched by an agent (depth 2) is never reused"
        )
        await db.create_run(_run(keeps, "run-5", agent_hash=_hash("agent 2")))
        assert await db.find_reusable_run(keeps, _hash("agent 2"), _hash("input")) is None, "only a SUCCESS run matches"
