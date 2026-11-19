"""The runnable examples at least build: their agents are valid definitions (sub-agents, skills, schemas). No model calls."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from fakes import FakeSettings, Reactive, Scripted, structured_reply, use_model

from micro.jobs import run_job
from micro.model_settings import ModelSettings
from micro.models import AgentInput, AgentOutput, RunStatus
from micro.runner import AgentRunner

EXAMPLES = Path(__file__).parent.parent / "examples"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / name / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_launching_example_has_an_editor_that_can_launch_exactly_two_agents():
    editor = load("launching").Editor()
    assert [sub().name for sub in editor.subagents] == ["summariser", "translator"]
    assert editor.model.model == "gemini-3.5-flash-lite"
    assert "- summariser:" in editor.build_system_prompt() and "- translator:" in editor.build_system_prompt()


def test_the_skills_example_has_an_assistant_with_the_three_skills():
    assistant = load("skills").Assistant()
    assert sorted(skill.name for skill in assistant.skills) == ["email-reply", "meeting-notes", "release-notes"]
    assert assistant.model.model == "gemini-3.5-flash-lite"


@pytest.mark.parametrize("name", ["launching", "skills", "mcp"])
def test_the_examples_ask_for_a_request_or_use_their_samples(name):
    module = load(name)
    assert module.SAMPLES and all(isinstance(sample, str) and sample for sample in module.SAMPLES)


def test_the_mcp_example_reads_github_with_a_token_by_name_and_six_read_only_tools():
    agent = load("mcp").RepoReader()
    (server,) = agent.mcp_servers
    assert server.url == "https://api.githubcopilot.com/mcp/readonly" and server.bearer_from == "GITHUB_TOKEN"
    assert server.tools is not None and len(server.tools) == 6 and server.discover is False
    assert not any(word in tool for tool in server.tools for word in ("create", "update", "delete", "merge", "push"))
    assert agent.model.model == "gemini-3.5-flash-lite"


def test_the_doclearn_agents_build_and_are_valid_definitions():
    """The example's five agents are valid definitions: they build, and their settings, schemas and tools are consistent.
    The example has no tests of its own; `make pipeline` runs it for real."""
    from doclearn.alignment import SemanticAlignmentAgent
    from doclearn.analyzer import DocumentAnalyzer
    from doclearn.autofill import FormFillerAgent
    from doclearn.generation import GeneratorAgent
    from doclearn.reconciler import ReconciliationAgent

    classes = [DocumentAnalyzer, ReconciliationAgent, SemanticAlignmentAgent, GeneratorAgent, FormFillerAgent]
    agents = [cls() for cls in classes]
    assert len({agent.name for agent in agents}) == len(agents)
    for agent in agents:
        assert agent.model.model in agent.model.models
        assert issubclass(agent.input_model, AgentInput) and agent.input_model is not AgentInput  # its own input
        assert issubclass(agent.output_model, AgentOutput) and agent.output_model is not AgentOutput
        assert agent.build_system_prompt() and agent.system_prompt_hash
        assert len(set(agent.tool_names)) == len(agent.tool_names)


def test_the_resume_example_uses_the_smallest_claude_model_with_thinking_off():
    example = load("resume")
    for agent in (example.Summariser(), example.ReleaseNotes()):
        assert (agent.model.provider, agent.model.model) == ("anthropic", "claude-haiku-5-5")
        assert agent.model.label() == "off"


async def test_the_resume_example_crashes_after_the_summaries_and_the_resumed_run_reuses_them(db):
    """The example's own pipeline, on fake models: the first run raises after the summaries, and the second run, with
    `resume_from`, answers the summaries from the log and runs only the notes."""
    example = load("resume")

    class FakeSummariser(example.Summariser):
        model: ModelSettings = FakeSettings(key="summaries")

    class FakeWriter(example.ReleaseNotes):
        model: ModelSettings = FakeSettings(key="notes")

    summaries = Reactive(respond=lambda text, n: structured_reply("SummaryOutput", {"summary": "A change."}))
    notes = Scripted(script=[structured_reply("NotesOutput", {"notes": "The notes."})])
    use_model(summaries, "summaries")
    use_model(notes, "notes")
    runner = AgentRunner(db, retry_delay_s=0)
    agents = {"summariser": FakeSummariser, "writer": FakeWriter}
    commits = example.COMMITS

    first = await run_job(db, "t", lambda job_id: example.pipeline(runner, job_id, commits, crash=True, **agents))
    assert first.status == RunStatus.FAILED and "simulated crash" in (first.error or "")
    assert [r.agent_name for r in await db.list_runs(job_id=first.job_id)] == ["summariser"] * len(commits)

    calls_before = sum(summaries.counts.values())
    second = await run_job(
        db, "t", lambda job_id: example.pipeline(runner, job_id, commits, **agents), resume_from=first.job_id
    )
    assert second.status == RunStatus.SUCCESS and second.result == "The notes."
    assert sum(summaries.counts.values()) == calls_before  # the summaries were not asked for again
    assert notes.calls == 1
    rows = await db.list_runs(job_id=second.job_id)
    reused = [r for r in rows if r.reused_from_agent_run_id]
    assert [r.agent_name for r in reused] == ["summariser"] * len(commits)
    assert all((r.input_tokens, r.output_tokens) == (0, 0) for r in reused)
    assert [r.agent_name for r in rows if not r.reused_from_agent_run_id] == ["release_notes"]


# the chat example: a web app over a streaming agent. The model is a fake, and nothing leaves the process.


async def test_the_chat_example_is_a_streaming_support_agent_on_the_smallest_claude_model():
    chat = load("chat")
    agent = chat.Support()
    assert agent.streaming and [t.name for t in agent.tools] == ["lookup_order"]
    assert (agent.model.model, agent.model.thinking) == ("claude-haiku-5-5", "off")
    assert chat.lookup_order.invoke({"order_id": "1001"}).startswith("Order 1001")
    assert "no order 9" in chat.lookup_order.invoke({"order_id": "9"})


def sse(text: str) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in text.split("\n\n") if line.startswith("data: {")]


async def chat_app(db, model, **kwargs):
    from fakes import runner_with

    chat = load("chat")

    class Fake(chat.Support):
        model: ModelSettings = FakeSettings()

    runner = runner_with(db, model, **kwargs)
    store = chat.Store()
    return chat.make_app(runner, store, agent=Fake), runner, store


def client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_the_chat_example_streams_ag_ui_and_vercel_to_a_client_that_sends_the_conversation(db):
    from fakes import Streams

    app, _, _ = await chat_app(db, Streams(turns=[["Hello ", "there."], ["Hi again."]]))
    agui_body = {"threadId": "t", "runId": "r", "messages": [{"id": "1", "role": "user", "content": "hi"}]}
    async with client(app) as http:
        response = await http.post("/agui", json=agui_body)
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        events = sse(response.text)
        assert events[0]["type"] == "RUN_STARTED" and events[-1]["type"] == "RUN_FINISHED"
        assert "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT") == "Hello there."

        response = await http.post(
            "/vercel",
            json={"id": "c", "messages": [{"id": "u", "role": "user", "parts": [{"type": "text", "text": "hi"}]}]},
        )
        parts = sse(response.text)
        assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
        assert (
            parts[0]["type"] == "start" and parts[-1]["type"] == "finish" and response.text.endswith("data: [DONE]\n\n")
        )


async def test_the_chat_example_answers_a_bad_request_with_a_422(db):
    from fakes import Streams

    app, _, _ = await chat_app(db, Streams(turns=[]))
    async with client(app) as http:
        assert (await http.post("/agui", content="not json")).status_code == 422
        assert (await http.post("/vercel", json={"messages": []})).status_code == 422
        assert (await http.post("/chat/t", json={"nope": 1})).status_code == 422
        assert (await http.post("/stop/nothing")).status_code == 404


async def test_the_chat_example_keeps_the_conversation_for_a_client_that_sends_only_a_message(db):
    from fakes import Streams, tool_call_chunk

    model = Streams(
        turns=[
            [tool_call_chunk("lookup_order", {"order_id": "1001"}, "c1")],
            ["It arrives Thursday."],
            ["Anything else?"],
        ]
    )
    app, _, store = await chat_app(db, model)
    async with client(app) as http:
        first = sse((await http.post("/chat/t1", json={"message": "where is order 1001?"})).text)
        assert [e["toolCallName"] for e in first if e["type"] == "TOOL_CALL_START"] == ["lookup_order"]
        await http.post("/chat/t1", json={"message": "thanks"})
        stored = (await http.get("/chat/t1")).json()

    assert [m["role"] for m in stored] == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert "Order 1001" in stored[2]["content"]
    assert [m.content for m in model.seen[2] if m.type == "human"] == [
        "where is order 1001?",
        "thanks",
    ]  # the second turn had the first
    assert len(store.messages("t1")) == 6


async def test_the_chat_example_stops_a_response_and_keeps_what_was_said(db):
    from fakes import Hold, Streams

    gate = Hold()
    app, runner, store = await chat_app(db, Streams(turns=[["Let me check ", gate, "the whole order."]]))
    async with client(app) as http:
        request = asyncio.create_task(http.post("/chat/t2", json={"message": "where is my order?"}))
        await gate.reached.wait()
        await asyncio.sleep(0.03)
        (row,) = await db.list_runs()
        assert (await http.post(f"/stop/{row.agent_run_id}")).json() == {"stopped": True}
        events = sse((await request).text)

    assert events[-1]["type"] == "RUN_FINISHED" and events[-1]["outcome"]["type"] == "cancelled"
    assert [m.content for m in store.messages("t2")] == ["where is my order?", "Let me check "]
    assert (await db.get_run(row.agent_run_id)).status is RunStatus.CANCELLED


async def test_the_chat_example_saves_the_turn_when_the_client_leaves(db):
    from fakes import Hold, Streams

    gate = Hold()
    app, _, store = await chat_app(db, Streams(turns=[["Half of ", gate, "an answer."]]))
    body = json.dumps({"message": "hello"}).encode()
    gone = asyncio.Event()
    sent: list[dict] = []
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat/t3",
        "raw_path": b"/chat/t3",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
        "root_path": "",
    }
    call = asyncio.create_task(app(scope, receive, send))
    await gate.reached.wait()
    await asyncio.sleep(0.03)
    gone.set()  # the client closes the connection
    await asyncio.wait_for(call, timeout=5)

    assert [m.content for m in store.messages("t3")] == ["hello", "Half of "]
    (row,) = await db.list_runs()
    assert row.status is RunStatus.CANCELLED
