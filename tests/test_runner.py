import json
import logging

import pytest
from fakes import Echo, EchoInput, EchoOutput, FakeSettings, Scripted, runner_with, structured_reply, tool_reply
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, tool

from featherweight import runner as runner_module
from featherweight.model_settings import Gemini, ModelSettings
from featherweight.models import AgentInput, RunStatus
from featherweight.runner import AgentRunner, ModelSetupError, is_non_retryable


@tool
def shout(text: str) -> str:
    """Upper-case the text."""
    return text.upper()


class EchoWithTool(Echo):
    name: str = "echo_tool"
    tools: list[BaseTool] = [shout]


class Limited(EchoWithTool):
    name: str = "limited"
    recursion_limit: int = 3


OK = lambda text="hi", **kw: structured_reply("EchoOutput", {"echoed": text}, **kw)  # noqa: E731


async def test_success(db):
    job_id = await db.create_job("learn")
    model = Scripted(script=[OK(model="gemini-3.5-flash-lite")])
    result = await runner_with(db, model).run(Echo, EchoInput(text="hello"), job_id=job_id)

    assert result.status == RunStatus.SUCCESS
    assert isinstance(result.output, EchoOutput)
    assert result.output.echoed == "hi"
    assert result.error is None
    assert (result.input_tokens, result.output_tokens) == (5, 7)
    assert result.attempts == 1
    assert result.model_name == "gemini-3.5-flash-lite"
    assert result.latency_ms > 0

    rows = await db.list_runs(job_id=job_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.agent_run_id == result.agent_run_id
    assert row.status == RunStatus.SUCCESS
    assert row.input == {"text": "hello"}
    assert row.output == {"open_questions": [], "echoed": "hi"}
    assert (row.input_tokens, row.output_tokens) == (5, 7)
    assert row.reported_model_name == "gemini-3.5-flash-lite"
    assert row.system_prompt_hash == Echo().system_prompt_hash
    assert row.model_name == "fake-model"  # what the agent's settings name, as against what the API reported
    assert (row.provider, row.reasoning) == ("fake", "")
    assert row.settings == {"model": "fake-model", "max_output_tokens": None, "show_thoughts": False, "key": "default"}
    assert row.completed_at is not None

    messages = model.seen[0]  # what the model was sent
    system = [m for m in messages if isinstance(m, SystemMessage)][0]
    user = [m for m in messages if isinstance(m, HumanMessage)][0]
    assert system.content == Echo().build_system_prompt()
    assert json.loads(user.content) == {"text": "hello"}


async def test_open_questions_are_not_a_failure(db):
    reply = structured_reply("EchoOutput", {"echoed": None, "open_questions": ["Which date?"]})
    result = await runner_with(db, Scripted(script=[reply])).run(Echo, EchoInput(text="x"))
    assert result.status == RunStatus.SUCCESS
    assert result.output.open_questions == ["Which date?"]
    assert result.output.echoed is None


@pytest.mark.parametrize(
    "make_error,error_prefix",
    [
        (lambda: RuntimeError("flaky"), "RuntimeError: flaky"),
        (lambda: google_error(*RATE_LIMIT), "ChatGoogleGenerativeAIError"),
    ],
    ids=["runtime_error", "google_429"],
)
async def test_retry_then_success(db, make_error, error_prefix):
    model = Scripted(script=[make_error(), OK()])
    result = await runner_with(db, model).run(Echo, EchoInput(text="x"))

    assert result.status == RunStatus.SUCCESS
    assert result.attempts == 2

    rows = await db.list_runs()
    assert [r.attempt for r in rows] == [1, 2]
    assert len({r.invocation_id for r in rows}) == 1
    assert len({r.agent_run_id for r in rows}) == 2
    assert rows[0].status == RunStatus.FAILED
    assert rows[0].error.startswith(error_prefix)
    assert rows[0].output is None
    assert rows[1].status == RunStatus.SUCCESS
    assert result.agent_run_id == rows[1].agent_run_id


@pytest.mark.parametrize("max_attempts", [2, 3])
async def test_retries_exhausted_returns_failed_result(db, monkeypatch, max_attempts):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(runner_module.asyncio, "sleep", fake_sleep)
    names = ["one", "two", "three"]
    model = Scripted(script=[RuntimeError(n) for n in names] + [OK()])  # an extra success that must never be reached
    result = await runner_with(db, model, max_attempts=max_attempts, retry_delay_s=0.5).run(Echo, EchoInput(text="x"))

    assert result.status == RunStatus.FAILED
    assert result.output is None
    assert result.error == f"RuntimeError: {names[max_attempts - 1]}"
    assert result.attempts == max_attempts == model.calls
    assert sleeps == [0.5] * (max_attempts - 1)  # waits between attempts, not after the last one

    rows = await db.list_runs()
    assert [r.status for r in rows] == [RunStatus.FAILED] * max_attempts
    assert [r.error for r in rows] == [f"RuntimeError: {n}" for n in names[:max_attempts]]
    assert result.agent_run_id == rows[-1].agent_run_id
    assert rows[-1].input == {"text": "x"}  # the input is kept, so a failed run can be resumed


class HttpError(Exception):
    def __init__(self, code):
        super().__init__(f"http {code}")
        self.code = code


def wrapped(code):
    try:
        try:
            raise HttpError(code)
        except HttpError as inner:
            raise RuntimeError("Error calling model") from inner
    except RuntimeError as outer:
        return outer


def real_google_error(wrapper_cls, code, status):
    """The error shape langchain-google-genai raises: a wrapper caused by a google-genai API error."""
    from google.genai.errors import ClientError, ServerError

    api_cls = ServerError if code >= 500 else ClientError
    wrapper = wrapper_cls(f"Error calling model ({status}): {code}")
    wrapper.__cause__ = api_cls(code, {"error": {"code": code, "status": status}}, None)
    return wrapper


def google_error(name, code, status):
    from langchain_google_genai import chat_models

    return real_google_error(getattr(chat_models, name), code, status)


BAD_KEY = ("GoogleInvalidRequestError", 400, "INVALID_ARGUMENT")
UNKNOWN_MODEL = ("GoogleModelNotFoundError", 404, "NOT_FOUND")
FORBIDDEN = ("ChatGoogleGenerativeAIError", 403, "PERMISSION_DENIED")
RATE_LIMIT = ("ChatGoogleGenerativeAIError", 429, "RESOURCE_EXHAUSTED")
UNAVAILABLE = ("ChatGoogleGenerativeAIError", 503, "UNAVAILABLE")


@pytest.mark.parametrize(
    "make_error,non_retryable",
    [
        *[(lambda c=c: HttpError(c), True) for c in (400, 404)],
        *[(lambda c=c: wrapped(c), True) for c in (401, 403)],  # found through the cause chain
        *[(lambda c=c: HttpError(c), False) for c in (408, 429, 503)],
        (lambda: wrapped(429), False),
        (lambda: google_error(*BAD_KEY), True),
        (lambda: google_error(*UNKNOWN_MODEL), True),
        (lambda: google_error(*FORBIDDEN), True),
        (lambda: google_error(*RATE_LIMIT), False),
        (lambda: google_error(*UNAVAILABLE), False),
        (lambda: RuntimeError("boom"), False),
        (lambda: ModelSetupError("no key"), True),
    ],
)
def test_is_non_retryable(make_error, non_retryable):
    assert is_non_retryable(make_error()) is non_retryable


@pytest.mark.parametrize(
    "make_error",
    [lambda: wrapped(401), lambda: wrapped(400), lambda: wrapped(403), lambda: google_error(*BAD_KEY)],
)
async def test_non_retryable_error_fails_fast_without_waiting(db, monkeypatch, make_error):
    async def no_sleep(delay):
        raise AssertionError("must not wait before a non-retryable failure")

    monkeypatch.setattr(runner_module.asyncio, "sleep", no_sleep)
    model = Scripted(script=[make_error(), OK()])
    result = await runner_with(db, model).run(Echo, EchoInput(text="x"))

    assert result.status == RunStatus.FAILED
    assert result.attempts == 1
    assert model.calls == 1
    assert len(await db.list_runs()) == 1


class RealEcho(Echo):
    model: ModelSettings = Gemini()  # a real provider: conftest has removed the API keys, so it cannot be built


class BrokenSettings(FakeSettings):
    def build(self):
        raise ValueError("API key required")


class BrokenEcho(Echo):
    model: ModelSettings = BrokenSettings()


@pytest.mark.parametrize("agent", [RealEcho, BrokenEcho], ids=["no_api_key", "build_raises"])
async def test_model_setup_error_fails_fast_and_is_returned_not_raised(db, agent):
    # No network is touched: the real provider fails at setup, and the broken one raises.
    result = await AgentRunner(db, retry_delay_s=0.0).run(agent, EchoInput(text="x"))

    assert result.status == RunStatus.FAILED
    assert result.attempts == 1
    assert "ModelSetupError" in result.error
    assert (await db.get_run(result.agent_run_id)).status == RunStatus.FAILED


async def test_missing_structured_response_is_a_failed_attempt(db, monkeypatch):
    class StubAgent:
        calls = 0

        async def ainvoke(self, *args, **kwargs):
            StubAgent.calls += 1
            if StubAgent.calls == 1:
                return {"messages": [AIMessage(content="just text")]}  # no structured_response
            return {
                "messages": [AIMessage(content="")],
                "structured_response": EchoOutput(echoed="ok"),
            }

    monkeypatch.setattr(runner_module, "create_agent", lambda *a, **k: StubAgent())
    result = await runner_with(db, Scripted(script=[])).run(Echo, EchoInput(text="x"))

    assert result.status == RunStatus.SUCCESS
    assert result.attempts == 2
    first = (await db.list_runs())[0]
    assert first.status == RunStatus.FAILED
    assert "structured response" in first.error


async def test_tool_loop_sums_tokens_across_model_calls(db):
    model = Scripted(
        script=[
            tool_reply("shout", {"text": "hi"}, "t1", input_tokens=10, output_tokens=4),
            OK("HI", input_tokens=20, output_tokens=6),
        ]
    )
    result = await runner_with(db, model).run(EchoWithTool, EchoInput(text="hi"))

    assert result.status == RunStatus.SUCCESS
    assert result.output.echoed == "HI"
    assert (result.input_tokens, result.output_tokens) == (30, 10)
    assert (await db.get_run(result.agent_run_id)).tool_names == ["shout"]


async def test_recursion_limit_stops_a_runaway_tool_loop(db):
    script = [tool_reply("shout", {"text": "x"}, f"t{i}") for i in range(60)]
    model = Scripted(script=script)
    result = await runner_with(db, model, max_attempts=1).run(Limited, EchoInput(text="x"))

    assert result.status == RunStatus.FAILED
    assert "GraphRecursionError" in result.error
    assert model.calls < 60


async def test_wrong_input_type_raises_and_writes_nothing(db):
    class Other(AgentInput):
        text: str

    with pytest.raises(TypeError):
        await runner_with(db, Scripted(script=[OK()])).run(Echo, Other(text="x"))
    assert await db.list_runs() == []


async def test_lifecycle_is_logged(db, caplog):
    caplog.set_level(logging.INFO, logger="featherweight")
    model = Scripted(script=[RuntimeError("flaky"), OK()])
    await runner_with(db, model, retry_delay_s=0.0).run(Echo, EchoInput(text="x"))

    messages = [r.getMessage() for r in caplog.records]
    starts = [m for m in messages if m.startswith("invocation_started")]
    assert starts and "agent=echo" in starts[0] and "max_attempts=3" in starts[0]
    assert sum(m.startswith("attempt_started") for m in messages) == 2
    failed = [r for r in caplog.records if r.getMessage().startswith("attempt_failed")]
    assert failed and failed[0].levelno == logging.WARNING
    assert "non_retryable=False" in failed[0].getMessage()
    assert any(m.startswith("retry_waiting") for m in messages)
    done = [m for m in messages if m.startswith("invocation_finished")]
    assert done and "status=SUCCESS" in done[0] and "attempts=2" in done[0]


# AgentBase.check_output: rules that need the input and the output


async def test_check_output_receives_the_input_and_the_output_and_a_rejection_is_retried(db):
    seen = []

    class Picky(Echo):
        name: str = "picky"

        def check_output(self, agent_input, output):
            seen.append((agent_input.text, output.echoed))
            if len(seen) == 1:
                raise ValueError("not good enough")

    model = Scripted(script=[OK("one"), OK("two")])
    result = await runner_with(db, model).run(Picky, EchoInput(text="hello"))

    assert result.status == RunStatus.SUCCESS and result.attempts == 2
    assert seen == [("hello", "one"), ("hello", "two")]
    rows = await db.list_runs()
    assert rows[0].status == RunStatus.FAILED and rows[0].error == "ValueError: not good enough"
    assert rows[1].status == RunStatus.SUCCESS
