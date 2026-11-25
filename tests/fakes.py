"""Test doubles for the chat model. No test makes a real LLM call."""

import asyncio
import json
import os
from contextlib import contextmanager
from typing import Any, ClassVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel

from featherweight.jobs import Job, run_job
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput
from featherweight.runner import AgentRunner

MODELS: dict[str, BaseChatModel] = {}  # the scripted chat models of the running test, by key; emptied for each test


class FakeSettings(ModelSettings):
    """Model settings for tests. `build()` returns the scripted chat model the test registered under `key`, so a test
    agent is declared with model settings like a production agent, and the runner takes the production path:
    `agent.model.build()`."""

    provider: ClassVar[str] = "fake"
    model: str = "fake-model"
    key: str = "default"  # which scripted model of the test this agent gets

    def build(self):
        if self.key not in MODELS:
            raise AssertionError(
                f"no fake model {self.key!r} in this test: use runner_with(db, model) or use_model(model)"
            )
        return MODELS[self.key]


@contextmanager
def settings(**values: object):
    """Set featherweight's environment settings while an object is built (they are read when it is): `settings(job_lease=0.3)` is
    `FTW_JOB_LEASE=0.3`. The values are put back afterwards."""
    before = {key: os.environ.get(key) for key in (f"FTW_{name.upper()}" for name in values)}
    for name, value in values.items():
        os.environ[f"FTW_{name.upper()}"] = str(value)
    try:
        yield
    finally:
        for key, old in before.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def use_model(model: BaseChatModel, key: str = "default") -> None:
    """From now on, agents whose settings have this `key` are run by `model`."""
    MODELS[key] = model


class Nothing(BaseModel):
    """The input and the output of a job that is only a pipeline in a test."""


async def run_pipeline(db, name: str, pipeline, **kwargs):
    """Run `pipeline(job_id)` as a job class, in this process: for the tests whose subject is the pipeline and what it does to the
    run log. The returned outcome has what the pipeline returned in `result`. The job's own output is not kept (`results=None`),
    so that a test of the result store counts only the steps."""
    returned: list = []

    async def run(self, ctx, input):
        returned.append(await pipeline(ctx.job_id))
        return Nothing()

    pipeline_job = type(
        "Pipeline", (Job,), {"name": name, "version": "1", "input_model": Nothing, "output_model": Nothing, "run": run}
    )
    outcome = await run_job(AgentRunner(db, results=None), pipeline_job, Nothing(), **kwargs)
    return outcome.model_copy(update={"result": returned[0] if returned else None})


def runner_with(db, model, **kwargs) -> AgentRunner:
    """An AgentRunner on `db`, whose agents run on `model`, with no delay between retries."""
    use_model(model)
    kwargs.setdefault("retry_delay_s", 0.0)
    return AgentRunner(db, **kwargs)


class EchoInput(AgentInput):
    text: str


class EchoOutput(AgentOutput):
    echoed: str | None = None


class Echo(AgentBase):
    name: str = "echo"
    description: str = "Echoes the input"
    version: str = "0.1.0"
    prompt: str = "Echo the input text."
    model: ModelSettings = FakeSettings()
    input_model: ClassVar[type[AgentInput]] = EchoInput
    output_model: ClassVar[type[AgentOutput]] = EchoOutput


class Scripted(BaseChatModel):
    """A chat model that replays a script. Items are AIMessages, or exceptions to raise."""

    script: list[Any]
    calls: int = 0
    seen: list[Any] = []
    bound: list[list[str]] = []  # the names of the tools the model was bound to, for each time it was bound

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound.append([getattr(t, "name", str(t)) for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


def structured_reply(output_name: str, args: dict, *, input_tokens=5, output_tokens=7, model="fake-model"):
    return AIMessage(
        content="",
        tool_calls=[{"name": output_name, "args": args, "id": f"call-{output_name}"}],
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        response_metadata={"model_name": model},
    )


def tool_reply(name: str, args: dict, call_id: str, *, input_tokens=3, output_tokens=2):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


class Reactive(BaseChatModel):
    """A chat model that decides each reply from the user message, for concurrency tests.

    `respond(user_text, call_number)` returns an AIMessage or an exception to raise, where
    `call_number` counts calls for that same user text. It yields to the event loop for
    `delay_s` on each call, so concurrent runs really interleave, and it records the most calls
    that were in flight at once.
    """

    respond: Any
    delay_s: float = 0.01
    in_flight: int = 0
    max_in_flight: int = 0
    counts: dict[str, int] = {}

    @property
    def _llm_type(self) -> str:
        return "reactive"

    def bind_tools(self, tools, **kwargs):
        return self

    def _reply(self, messages) -> ChatResult:
        text = [m for m in messages if isinstance(m, HumanMessage)][-1].content
        self.counts[text] = self.counts.get(text, 0) + 1
        item = self.respond(text, self.counts[text])
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._reply(messages)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay_s)
            return self._reply(messages)
        finally:
            self.in_flight -= 1


def respond(user_text, call_number):
    """`Reactive` reply for `Echo`: `ok-*` succeeds, `flaky-*` fails on its first call, `fail-*` fails every time."""
    text = json.loads(user_text)["text"]
    kind = text.split("-")[0]
    if kind == "fail" or (kind == "flaky" and call_number == 1):
        return RuntimeError(f"down {text}")
    return structured_reply("EchoOutput", {"echoed": text})


def flat_blocks(nodes: list[dict]) -> list[dict]:
    """Flatten nested {"type", "text", "children"} dicts into the Analyzer's flat blocks (ids and parent ids)."""
    blocks: list[dict] = []

    def add(node: dict, parent: str) -> None:
        block_id = f"n{len(blocks) + 1}"
        blocks.append({"id": block_id, "parent_id": parent, "type": node["type"], "text": node["text"]})
        for child in node.get("children", []):
            add(child, block_id)

    for node in nodes:
        add(node, "")
    return blocks


class Hold:
    """A point in a script where a fake stops until the test lets it go: `await hold.reached.wait()` says the stream is
    there, `hold.release.set()` lets it go on. In `Streams.turns`, or awaited by a tool."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.reached.set()
        await self.release.wait()


class Streams(BaseChatModel):
    """A chat model that streams a script. Each model call streams the next turn of `turns`: a list of pieces of text,
    each piece one chunk, or an exception to raise. A piece may also be an `AIMessageChunk` (for a tool call). The last
    chunk of a turn carries `usage` as (input, output) tokens. `seen` holds the messages of each call."""

    turns: list[Any]
    usage: tuple[int, int] = (3, 4)
    delay_s: float = 0.0
    calls: int = 0
    seen: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "streams"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("a streaming agent must stream")

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        turn = self.turns[self.calls]
        turn_id = f"turn-{self.calls}"
        self.calls += 1
        for index, piece in enumerate(turn):
            if isinstance(piece, Exception):
                raise piece
            if isinstance(piece, Hold):
                await piece.wait()
                continue
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            last = index == len(turn) - 1
            usage = {"input_tokens": self.usage[0], "output_tokens": self.usage[1], "total_tokens": sum(self.usage)}
            if isinstance(piece, AIMessageChunk):
                chunk = piece.model_copy(update={"id": turn_id, "usage_metadata": usage if last else None})
            else:
                chunk = AIMessageChunk(content=piece, id=turn_id, usage_metadata=usage if last else None)
            yield ChatGenerationChunk(message=chunk)


def raw_call_chunk(name: str, args_text: str, call_id: str) -> AIMessageChunk:
    """A tool call chunk whose arguments are the text given, which may be half of a JSON."""
    return AIMessageChunk(content="", tool_call_chunks=[{"name": name, "args": args_text, "id": call_id, "index": 0}])


def tool_call_chunk(name: str, args: dict, call_id: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="", tool_call_chunks=[{"name": name, "args": json.dumps(args), "id": call_id, "index": 0}]
    )
