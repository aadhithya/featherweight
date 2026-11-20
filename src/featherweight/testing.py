"""Helpers for testing code that uses featherweight, and for checking an implementation of one of its abstract classes."""

import asyncio
import contextlib
import copy
import hashlib
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

from featherweight.db import Database, RunEnd, RunStart
from featherweight.models import RunStatus
from featherweight.observers import (
    AttemptEnd,
    AttemptStart,
    JobEnd,
    JobStart,
    JobSubmitted,
    ModelCallEnd,
    ModelCallStart,
    Observer,
    ReusedRun,
    Scope,
    ToolCallEnd,
    ToolCallStart,
)
from featherweight.queue import Delivery, JobMessage, JobQueue
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


async def _check_taking_a_job(db: Database) -> None:
    """Taking a queued job and cancelling it are compare-and-set steps: exactly one of them wins."""
    taken = await db.create_job("take")
    assert await db.start_job(taken) is True, "start_job takes a queued job"
    row = await db.get_job(taken)
    assert row is not None and row.status is RunStatus.RUNNING and row.started_at is not None
    assert await db.start_job(taken) is False, "start_job does not take a job twice"
    assert await db.cancel_queued_job(taken) is False, "a job that was taken cannot be cancelled while queued"
    assert [e.type for e in await db.list_job_events(taken)] == ["queued", "running"], "a refused step writes no event"

    asked = await db.create_job("asked")
    assert await db.request_job_cancel(asked) is True
    assert await db.start_job(asked) is False, "start_job does not take a job that was asked to be cancelled"
    assert await db.cancel_queued_job(asked) is True
    cancelled = await db.get_job(asked)
    assert cancelled is not None and cancelled.status is RunStatus.CANCELLED and cancelled.completed_at is not None
    assert [e.type for e in await db.list_job_events(asked)] == ["queued", "cancel_requested", "cancelled"]
    assert await db.cancel_queued_job(asked) is False and await db.start_job(asked) is False, "a final job stays final"
    assert await db.start_job("no-such-job") is False and await db.cancel_queued_job("no-such-job") is False

    for _ in range(20):  # a race between a worker and a client: exactly one wins, and the job agrees with the winner
        racing = await db.create_job("race")
        started, cancelled_ = await asyncio.gather(db.start_job(racing), db.cancel_queued_job(racing))
        assert started != cancelled_, "start_job and cancel_queued_job must not both win, or both lose"
        row = await db.get_job(racing)
        assert row is not None and row.status is (RunStatus.RUNNING if started else RunStatus.CANCELLED)
        types = [e.type for e in await db.list_job_events(racing)]
        assert types == ["queued", "running" if started else "cancelled"], "the winner writes exactly one event"


async def _check_job_events(db: Database) -> None:
    """The events of a job and the request to cancel it."""
    job = await db.create_job("events", job_hash=_hash("job"), input_hash=_hash("in"))
    seen = await db.list_job_events(job)
    assert [(e.type, e.status) for e in seen] == [("queued", RunStatus.INIT)], "a new job has one `queued` event"
    assert seen[0].job_id == job and seen[0].at.tzinfo is not None and seen[0].error_type is None

    await db.set_job_status(job, RunStatus.RUNNING)
    assert await db.list_cancel_requested([job]) == set(), "a job nobody asked to cancel is not listed"
    assert await db.list_cancel_requested([]) == set()
    assert await db.request_job_cancel(job) is True
    assert await db.request_job_cancel(job) is True, "asking again is not an error"
    assert await db.list_cancel_requested([job, "no-such-job"]) == {job}, "a job that was asked to cancel is listed"
    row = await db.get_job(job)
    assert row is not None and row.cancel_requested is True and row.status is RunStatus.RUNNING
    await db.set_job_status(job, RunStatus.FAILED, error_type="ValueError", error_hash=_hash("boom"))
    events = await db.list_job_events(job)
    assert await db.list_cancel_requested([job]) == set(), "a final job is not listed, whatever its flag"
    assert [e.type for e in events] == ["queued", "running", "cancel_requested", "failed"], (
        "one event per change, none for a repeat"
    )
    assert [e.seq for e in events] == sorted({e.seq for e in events}), "seq is increasing"
    assert (events[-1].status, events[-1].error_type, events[-1].error_hash) == (
        RunStatus.FAILED,
        "ValueError",
        _hash("boom"),
    )
    assert events[2].status is RunStatus.RUNNING, "a cancel request keeps the status the job had"

    assert await db.request_job_cancel(job) is False, "a final job cannot be asked to cancel"
    assert await db.request_job_cancel("no-such-job") is False
    assert len(await db.list_job_events(job)) == 4, "a refused request writes no event"

    # the other statuses make their events, and a job going back to the queue makes a `queued` one
    other = await db.create_job("events")
    await db.set_job_status(other, RunStatus.RUNNING)
    await db.set_job_status(other, RunStatus.INIT)
    await db.set_job_status(other, RunStatus.RUNNING)
    await db.set_job_status(other, RunStatus.SUCCESS)
    third = await db.create_job("events")
    await db.set_job_status(third, RunStatus.CANCELLED)
    assert [e.type for e in await db.list_job_events(other)] == ["queued", "running", "queued", "running", "succeeded"]
    assert [e.type for e in await db.list_job_events(third)] == ["queued", "cancelled"]
    try:
        await db.set_job_status("no-such-job", RunStatus.RUNNING)
    except LookupError:
        pass
    assert await db.list_job_events("no-such-job") == [], "a refused change writes no event"

    # reading: after, limit, and the events of several jobs in one query
    first, second = events[0], events[1]
    assert [e.seq for e in await db.list_job_events(job, after=first.seq)] == [e.seq for e in events[1:]]
    assert [e.seq for e in await db.list_job_events(job, limit=2)] == [first.seq, second.seq]
    assert await db.list_job_events(job, after=events[-1].seq) == []
    polled = await db.poll_job_events([job, third], after=0)
    assert [e.seq for e in polled] == sorted(e.seq for e in polled), "one query, in order"
    assert {e.job_id for e in polled} == {job, third} and len(polled) == 6
    assert await db.poll_job_events([], after=0) == []
    assert {e.job_id for e in await db.poll_job_events([job], after=second.seq)} == {job}


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

        # the columns of a submitted job: its hashes, when it started, and the flag
        hashed = await db.create_job("p", job_version="2", job_hash=_hash("job"), input_hash=_hash("in"))
        row = await db.get_job(hashed)
        assert row is not None and (row.job_version, row.job_hash, row.input_hash) == ("2", _hash("job"), _hash("in"))
        assert row.started_at is None and row.cancel_requested is False
        plain = await db.get_job(job_id)
        assert plain is not None and plain.job_version is None and plain.job_hash is None and plain.input_hash is None
        await db.set_job_status(hashed, RunStatus.RUNNING)
        row = await db.get_job(hashed)
        assert row is not None and row.started_at is not None, "RUNNING sets started_at"
        await db.set_job_status(hashed, RunStatus.INIT)
        row = await db.get_job(hashed)
        assert row is not None and row.started_at is None and row.status is RunStatus.INIT, (
            "back to INIT clears started_at"
        )
        await db.set_job_status(hashed, RunStatus.RUNNING)
        await db.set_job_status(hashed, RunStatus.CANCELLED, error_type="Ignored", error_hash=_hash("x"))
        row = await db.get_job(hashed)
        assert row is not None and row.status is RunStatus.CANCELLED and row.completed_at is not None
        assert row.started_at is not None and row.error_type is None, (
            "CANCELLED is final, keeps started_at, has no error"
        )

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

        await _check_job_events(db)

        await _check_taking_a_job(db)


class MemoryQueue(JobQueue):
    """A `JobQueue` in memory: nothing survives the process. For tests, and the smallest example of the class."""

    def __init__(self) -> None:
        self._seq = 0
        self._ready: list[tuple[int, JobMessage]] = []
        self._claimed: dict[str, tuple[int, JobMessage]] = {}

    async def enqueue(self, message: JobMessage) -> None:
        self._seq += 1
        self._ready.append((self._seq, message.model_copy(deep=True)))

    async def receive(self, timeout: float) -> Delivery | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self._ready:
                seq, message = self._ready.pop(0)  # a claim is one step: no other receiver can take it too
                self._claimed[str(seq)] = (seq, message)
                return Delivery(message=message.model_copy(deep=True), token=str(seq))
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.005, remaining))

    async def ack(self, delivery: Delivery) -> None:
        self._claimed.pop(delivery.token, None)

    async def nack(self, delivery: Delivery, *, requeue: bool) -> None:
        claimed = self._claimed.pop(delivery.token, None)
        if claimed is not None and requeue:
            self._ready.append(claimed)
            self._ready.sort(key=lambda item: item[0])  # back in its place in the order


async def check_queue(make_queue: Callable[[], JobQueue | Awaitable[JobQueue]]) -> None:
    """Run the `JobQueue` contract against a queue. `make_queue` returns a fresh, empty queue (it may be async). Raises
    `AssertionError` that says which rule a queue breaks.

        async def test_my_queue():
            await check_queue(lambda: MyQueue(...))
    """

    async def fresh() -> JobQueue:
        made = make_queue()
        return await made if isinstance(made, Awaitable) else made

    def message(n: int, **input: Any) -> JobMessage:
        return JobMessage(job_id=f"job-{n}", job_name="shout", input=input or {"n": n})

    queue = await fresh()
    assert await queue.receive(0.05) is None, "receive on an empty queue must return None after the timeout"

    payload = {"text": "héllo", "n": 3, "ok": True, "none": None, "list": [1, 2.5, "x"], "nested": {"é": ["ü"]}}
    await queue.enqueue(JobMessage(job_id="job-a", job_name="shout", input=payload))
    got = await queue.receive(1.0)
    assert got is not None, "a message that was enqueued must be received"
    assert got.message == JobMessage(job_id="job-a", job_name="shout", input=payload), (
        "the message must come back equal as JSON"
    )
    assert await queue.receive(0.05) is None, (
        "a received message must be invisible to other receivers until it is acked or nacked"
    )
    await queue.ack(got)
    assert await queue.receive(0.05) is None, "an acked message must be gone"

    carrier = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", "tracestate": "ftw=é"}
    await queue.enqueue(JobMessage(job_id="job-t", job_name="shout", input={"n": 1}, trace_context=carrier))
    got = await queue.receive(1.0)
    assert got is not None and got.message.trace_context == carrier, "the trace context must come back unchanged"
    await queue.ack(got)
    await queue.enqueue(message(0))
    got = await queue.receive(1.0)
    assert got is not None and got.message.trace_context == {}, "a message without a trace context has an empty one"
    await queue.ack(got)

    queue = await fresh()
    for n in range(5):
        await queue.enqueue(message(n))
    order = []
    for _ in range(5):
        delivery = await queue.receive(1.0)
        assert delivery is not None
        order.append(delivery.message.job_id)
        await queue.ack(delivery)
    assert order == [f"job-{n}" for n in range(5)], "messages must come out in the order they went in"

    queue = await fresh()
    await queue.enqueue(message(1))
    await queue.enqueue(message(2))
    first = await queue.receive(1.0)
    assert first is not None and first.message.job_id == "job-1"
    await queue.nack(first, requeue=True)
    again = await queue.receive(1.0)
    assert again is not None and again.message.job_id == "job-1", (
        "nack(requeue=True) must make the message available again, in its place"
    )
    await queue.nack(again, requeue=False)
    rest = await queue.receive(1.0)
    assert rest is not None and rest.message.job_id == "job-2", "nack(requeue=False) must drop the message"
    await queue.ack(rest)
    assert await queue.receive(0.05) is None, "a dropped message must not come back"

    queue = await fresh()
    await queue.enqueue(message(1))
    await queue.enqueue(message(2))
    a, b = await asyncio.gather(queue.receive(1.0), queue.receive(1.0))
    assert a is not None and b is not None, "two receivers of two messages must both get one"
    assert {a.message.job_id, b.message.job_id} == {"job-1", "job-2"}, (
        "two concurrent receivers must get different messages"
    )
    assert a.token != b.token, "two deliveries must have different tokens"
    await queue.ack(a)
    await queue.ack(b)

    queue = await fresh()
    await asyncio.gather(*(queue.enqueue(message(n)) for n in range(40)))
    seen: list[str] = []

    async def worker() -> None:
        while (delivery := await queue.receive(0.1)) is not None:
            seen.append(delivery.message.job_id)
            await queue.ack(delivery)

    await asyncio.gather(*(worker() for _ in range(4)))
    assert sorted(seen) == sorted(f"job-{n}" for n in range(40)), (
        "many concurrent enqueues must each be delivered exactly once"
    )


class _RecordingScope(Scope):
    def __init__(self, observer: "RecordingObserver", kind: str):
        self._observer = observer
        self._kind = kind

    async def finish(self, end: JobEnd | AttemptEnd) -> None:
        self._observer.events.append((f"{self._kind}.finish", end))


class RecordingObserver(Observer):
    """An `Observer` that keeps what it hears, as `(hook, record)` pairs in `events`, for tests. It asks for no content:
    use `RecordingContentObserver` for one that does."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.carrier: dict[str, str] | None = None  # what `submitted` returns

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def records(self, name: str) -> list[Any]:
        return [record for hook, record in self.events if hook == name]

    @contextlib.asynccontextmanager
    async def _scope(self, kind: str, start: Any) -> AsyncGenerator[Scope]:
        self.events.append((f"{kind}.enter", start))
        yield _RecordingScope(self, kind)
        self.events.append((f"{kind}.exit", None))

    def job(self, start: JobStart) -> AbstractAsyncContextManager[Scope]:
        return self._scope("job", start)

    def attempt(self, start: AttemptStart) -> AbstractAsyncContextManager[Scope]:
        return self._scope("attempt", start)

    async def reused(self, run: ReusedRun) -> None:
        self.events.append(("reused", run))

    async def model_call_started(self, call: ModelCallStart) -> None:
        self.events.append(("model_call_started", call))

    async def model_call_finished(self, call: ModelCallEnd) -> None:
        self.events.append(("model_call_finished", call))

    async def tool_call_started(self, call: ToolCallStart) -> None:
        self.events.append(("tool_call_started", call))

    async def tool_call_finished(self, call: ToolCallEnd) -> None:
        self.events.append(("tool_call_finished", call))

    async def submitted(self, job: JobSubmitted) -> Mapping[str, str] | None:
        self.events.append(("submitted", job))
        return self.carrier


class RecordingContentObserver(RecordingObserver):
    capture_content = True


async def check_observer(make_observer: Callable[[], Observer | Awaitable[Observer]]) -> None:
    """Run the `Observer` contract against an observer: every hook accepts its record and fails nothing, a scope can be entered,
    finished and left, `submitted` returns nothing or a mapping of strings, and no hook changes the record it was given. Raises
    `AssertionError` that says which rule an observer breaks.

        async def test_my_observer():
            await check_observer(lambda: MyObserver(...))
    """
    made = make_observer()
    observer = await made if inspect.isawaitable(made) else made
    assert isinstance(observer, Observer), "an observer is a subclass of Observer"
    assert isinstance(observer.capture_content, bool), "capture_content is True or False"

    job = JobStart(job_id="job-1", name="job", version="1", job_hash=_hash("job"), input_hash=_hash("in"))
    attempt = AttemptStart(
        agent_run_id="run-1",
        invocation_id="inv-1",
        attempt=1,
        max_attempts=3,
        job_id="job-1",
        agent_name="agent",
        agent_version="1",
        agent_hash=_hash("agent"),
        provider="fake",
        model="fake-model",
    )
    job_end = JobEnd(status=RunStatus.SUCCESS)
    attempt_end = AttemptEnd(status=RunStatus.SUCCESS, final=True, input_tokens=3, output_tokens=2)
    events: list[tuple[str, Any]] = [
        (
            "reused",
            ReusedRun(
                agent_run_id="run-2",
                invocation_id="inv-1",
                reused_from_agent_run_id="run-0",
                agent_name="agent",
                agent_version="1",
                agent_hash=_hash("agent"),
            ),
        ),
        (
            "model_call_started",
            ModelCallStart(call_id="m-1", agent_run_id="run-1", provider="fake", model="fake-model"),
        ),
        ("model_call_finished", ModelCallEnd(call_id="m-1", agent_run_id="run-1", input_tokens=3, output_tokens=2)),
        ("tool_call_started", ToolCallStart(call_id="t-1", agent_run_id="run-1", tool_name="tool")),
        ("tool_call_finished", ToolCallEnd(call_id="t-1", agent_run_id="run-1", tool_name="tool")),
    ]
    before = [record.model_dump() for record in (job, attempt, job_end, attempt_end, *(r for _, r in events))]

    async with observer.job(job) as scope:
        assert isinstance(scope, Scope), "job() yields a Scope"
        async with observer.attempt(attempt) as attempt_scope:
            assert isinstance(attempt_scope, Scope), "attempt() yields a Scope"
            for hook, record in events:
                assert await getattr(observer, hook)(record) is None, f"{hook} returns nothing"
            await attempt_scope.finish(attempt_end)
        await scope.finish(job_end)
    carrier = await observer.submitted(JobSubmitted(job_id="job-1", name="job"))
    assert carrier is None or all(isinstance(k, str) and isinstance(v, str) for k, v in carrier.items()), (
        "submitted returns nothing, or a mapping of strings"
    )

    after = [record.model_dump() for record in (job, attempt, job_end, attempt_end, *(r for _, r in events))]
    assert before == after, "no hook changes the record it was given"
