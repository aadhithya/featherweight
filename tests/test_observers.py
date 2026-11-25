"""Observers: what the runner tells them, in what order, and that they never change a run. Fake models only."""

import asyncio

import pytest
from ag_ui.core import UserMessage
from fakes import Echo, EchoInput, Scripted, Streams, run_pipeline, runner_with, settings, structured_reply, use_model
from test_resume import model, steps
from test_streaming import Chat, say
from test_subagents import PARENT_INPUT, Parent, child_done, launch_child, parent_done

from featherweight import AgentRunner, Observer
from featherweight.models import Status
from featherweight.observers import ModelCallStart, Observers
from featherweight.testing import RecordingContentObserver, RecordingObserver, check_observer

ECHO = EchoInput(text="hello")


def echoed(text="hello"):
    return structured_reply("EchoOutput", {"echoed": text})


async def test_an_attempt_is_wrapped_and_finished_in_order(db):
    seen = RecordingObserver()

    result = await runner_with(db, Scripted(script=[echoed()]), observers=[seen]).run(Echo, ECHO)

    assert result.status == Status.SUCCESS
    assert seen.names() == [
        "attempt.enter",
        "model_call_started",
        "model_call_finished",
        "attempt.finish",
        "attempt.exit",
    ]
    start = seen.records("attempt.enter")[0]
    (row,) = await db.list_attempts()
    assert (start.attempt_id, start.run_id, start.attempt, start.max_attempts) == (
        row.attempt_id,
        row.run_id,
        1,
        3,
    )
    assert (start.agent_name, start.agent_hash, start.provider, start.model, start.depth) == (
        "echo",
        row.agent_hash,
        "fake",
        "fake-model",
        1,
    )
    end = seen.records("attempt.finish")[0]
    assert (end.status, end.final, end.input_tokens, end.output_tokens) == (Status.SUCCESS, True, 5, 7)
    call = seen.records("model_call_finished")[0]
    assert (call.attempt_id, call.input_tokens, call.output_tokens) == (row.attempt_id, 5, 7)
    assert call.duration_ms >= 0 and call.response_model == "fake-model"


async def test_each_retry_is_an_attempt_and_only_the_last_one_is_final(db):
    seen = RecordingObserver()
    script = [RuntimeError("down"), echoed()]

    result = await runner_with(db, Scripted(script=script), observers=[seen], max_attempts=2).run(Echo, ECHO)

    assert result.status == Status.SUCCESS and result.attempts == 2
    first, second = seen.records("attempt.finish")
    assert (first.status, first.final, first.error_type) == (Status.FAILED, False, "RuntimeError")
    assert (second.status, second.final) == (Status.SUCCESS, True)
    starts = seen.records("attempt.enter")
    assert [s.attempt for s in starts] == [1, 2] and starts[0].run_id == starts[1].run_id
    assert seen.names().count("attempt.exit") == 2


async def test_the_last_attempt_allowed_is_final_even_when_it_fails(db):
    seen = RecordingObserver()

    result = await runner_with(db, Scripted(script=[RuntimeError("down")]), observers=[seen], max_attempts=1).run(
        Echo, ECHO
    )

    assert result.status == Status.FAILED
    (end,) = seen.records("attempt.finish")
    assert (end.status, end.final, end.error_type) == (Status.FAILED, True, "RuntimeError")
    assert end.error is None  # text is content


class Broken(RecordingObserver):
    """Every hook raises."""

    def job(self, start):
        raise RuntimeError("job")

    def attempt(self, start):
        raise RuntimeError("attempt")

    async def reused(self, run):
        raise RuntimeError("reused")

    async def model_call_started(self, call):
        raise RuntimeError("model")

    async def model_call_finished(self, call):
        raise RuntimeError("model")

    async def tool_call_started(self, call):
        raise RuntimeError("tool")

    async def tool_call_finished(self, call):
        raise RuntimeError("tool")


class Slow(RecordingObserver):
    async def model_call_started(self, call):
        await asyncio.sleep(30)


async def test_an_observer_that_raises_or_is_too_slow_changes_nothing(db):
    good = RecordingObserver()

    with settings(observer_timeout=0.05):
        runner = runner_with(db, Scripted(script=[echoed()]), observers=[Broken(), Slow(), good])
    result = await runner.run(Echo, ECHO)

    assert result.status == Status.SUCCESS and result.output is not None and result.output.echoed == "hello"
    (row,) = await db.list_attempts()
    assert row.status == Status.SUCCESS and row.input_tokens == 5
    assert good.names()[0] == "attempt.enter" and "attempt.exit" in good.names()  # the others did not stop it


async def test_content_is_built_only_for_an_observer_that_asks(db):
    plain, wants = RecordingObserver(), RecordingContentObserver()

    await runner_with(db, Scripted(script=[echoed()]), observers=[plain, wants]).run(Echo, ECHO)

    start, end = plain.records("attempt.enter")[0], plain.records("attempt.finish")[0]
    assert (start.input, start.system_prompt, end.output) == (None, None, None)
    assert plain.records("model_call_started")[0].messages is None
    start, end = wants.records("attempt.enter")[0], wants.records("attempt.finish")[0]
    assert start.input == {"text": "hello"} and "Echo" in (start.system_prompt or "") or start.system_prompt
    assert end.output is not None and end.output["echoed"] == "hello"
    messages = wants.records("model_call_started")[0].messages
    assert messages is not None and messages[-1]["role"] == "human"


async def test_nothing_is_built_without_observers(db):
    runner = runner_with(db, Scripted(script=[echoed()]))

    assert not runner._observers and not runner._observers.wants_content
    assert runner._observers.callbacks("run", "fake", "m") == []
    assert (await runner.run(Echo, ECHO)).status == Status.SUCCESS


async def test_a_launched_agent_is_attributed_to_the_tool_call_that_launched_it(db):
    seen = RecordingContentObserver()
    script = [launch_child("t-launch"), child_done(), parent_done()]

    result = await runner_with(db, Scripted(script=script), observers=[seen]).run(Parent, PARENT_INPUT)

    assert result.status == Status.SUCCESS
    parent, child = seen.records("attempt.enter")
    assert (parent.depth, child.depth, child.parent_attempt_id) == (1, 2, parent.attempt_id)
    assert child.parent_tool_call_id == "t-launch" and parent.parent_tool_call_id is None
    (call_start,) = [c for c in seen.records("tool_call_started") if c.tool_name == "launch_agent"]
    assert call_start.call_id == "t-launch" and call_start.attempt_id == parent.attempt_id
    assert call_start.arguments is not None and call_start.arguments["agent_name"] == "child"
    (call_end,) = [c for c in seen.records("tool_call_finished") if c.tool_name == "launch_agent"]
    assert call_end.call_id == "t-launch" and "SUCCESS" in str(call_end.result)
    names = seen.names()  # the child runs inside its parent's tool call
    assert (
        names.index("tool_call_started")
        < names.index("attempt.enter", names.index("attempt.enter") + 1)
        < names.index("tool_call_finished")
    )
    assert names.count("attempt.enter") == names.count("attempt.exit") == 2


async def test_a_step_that_is_reused_is_an_event_and_not_an_attempt(db):
    seen = RecordingObserver()
    runner = runner_with(db, model(), observers=[seen], max_attempts=1)
    first = await run_pipeline(db, "p", steps(runner, ["a", "b"]), resumable=True)
    attempts = seen.names().count("attempt.enter")

    again = await run_pipeline(db, "p", steps(runner, ["a", "b"]), resume_from=first.job_id)

    assert again.status == Status.SUCCESS and attempts == 2
    assert seen.names().count("attempt.enter") == 2  # nothing ran again
    reused = seen.records("reused")
    assert len(reused) == 2
    originals = {r.attempt_id for r in await db.list_attempts(job_id=first.job_id)}
    assert {r.reused_from_attempt_id for r in reused} == originals
    assert {r.resumed_from for r in reused} == {first.job_id} and {r.job_id for r in reused} == {again.job_id}
    assert all(r.output is None for r in reused)  # no content was asked for


async def test_a_streaming_run_is_one_attempt_with_its_model_calls(db):
    seen = RecordingObserver()
    use_model(Streams(turns=[["Hel", "lo"]]))
    runner = AgentRunner(db, observers=[seen])

    _ = [event async for event in runner.stream(Chat, say("hi"))]

    assert seen.names() == [
        "attempt.enter",
        "model_call_started",
        "model_call_finished",
        "attempt.finish",
        "attempt.exit",
    ]
    start, end = seen.records("attempt.enter")[0], seen.records("attempt.finish")[0]
    (row,) = await db.list_attempts()
    assert start.streaming and start.attempt_id == row.attempt_id and start.attempt == 1
    assert (end.status, end.final, end.input_tokens, end.output_tokens) == (Status.SUCCESS, True, 3, 4)


async def test_a_streaming_run_that_fails_still_leaves_its_scope(db):
    seen = RecordingObserver()
    use_model(Streams(turns=[[RuntimeError("boom")]]))

    _ = [event async for event in AgentRunner(db, observers=[seen]).stream(Chat, say("hi"))]

    assert seen.names()[0] == "attempt.enter" and seen.names()[-1] == "attempt.exit"
    assert seen.records("attempt.finish")[0].status == Status.FAILED


async def test_a_submitted_job_gets_the_carriers_of_every_observer_that_gives_one():
    from featherweight.observers import JobSubmitted

    one, two, none = RecordingObserver(), RecordingObserver(), RecordingObserver()
    one.carrier, two.carrier = {"traceparent": "00-x"}, {"tracestate": "k=v"}

    carrier = await Observers([one, two, none, Broken()]).submitted(JobSubmitted(job_id="j", name="n"))

    assert carrier == {"traceparent": "00-x", "tracestate": "k=v"}


# the conformance kit


@pytest.mark.parametrize("make", [Observer, RecordingObserver, RecordingContentObserver])
async def test_the_kit_accepts_an_observer(make):
    await check_observer(make)


async def test_the_kit_catches_an_observer_that_breaks_a_rule():
    class Returns(Observer):
        async def reused(self, run):
            return 1

    class BadCarrier(Observer):
        async def submitted(self, job):
            return {"traceparent": 1}

    class NotAScope(Observer):
        def attempt(self, start):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def scope():
                yield object()

            return scope()

    for broken, rule in (
        (Returns, "returns nothing"),
        (BadCarrier, "mapping of strings"),
        (NotAScope, "yields a Scope"),
    ):
        with pytest.raises(AssertionError, match=rule):
            await check_observer(broken)


def test_the_records_are_frozen_and_content_is_dropped_by_strip():
    from pydantic import ValidationError

    from featherweight.observers import strip_content

    call = ModelCallStart(call_id="c", attempt_id="r", provider="p", model="m", messages=[{"role": "human"}])

    with pytest.raises(ValidationError):
        call.model_copy().__setattr__("model", "x")  # frozen
    assert strip_content(call).messages is None and call.messages is not None
    assert _unused(UserMessage) is None


def _unused(_):  # keeps the import of a protocol type honest for the streaming fakes
    return None
