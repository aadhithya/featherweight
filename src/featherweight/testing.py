"""Helpers for testing code that uses Featherweight, and for checking an implementation of one of its abstract classes."""

import asyncio
import copy
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

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
