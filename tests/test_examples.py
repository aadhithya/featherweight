"""The runnable examples at least build: their agents are valid definitions (sub-agents, skills, schemas). No model calls."""

import asyncio
import importlib.util
import json
import logging
import tempfile
from pathlib import Path

import pytest
from fakes import FakeSettings, Reactive, Scripted, Streams, structured_reply, tool_call_chunk, use_model
from langchain_core.messages import HumanMessage

from featherweight.client import JobClient
from featherweight.jobs import run_job
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentInput, AgentOutput, Status
from featherweight.queue import SqliteQueue
from featherweight.runner import AgentRunner
from featherweight.sqlite import SqliteDatabase
from featherweight.worker import JobWorker

EXAMPLES = Path(__file__).parent.parent / "examples"


def load(name: str, file: str = "agent.py"):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / name / file)
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


async def test_the_resume_example_crashes_after_the_summaries_and_the_resumed_run_reuses_them(db, monkeypatch):
    """The example's own pipeline, on fake models: the first run raises after the summaries, and the second run, with
    `resume_from`, answers the summaries from the log and runs only the notes."""
    example = load("resume")

    class FakeSummariser(example.Summariser):
        model: ModelSettings = FakeSettings(key="summaries")

    class FakeWriter(example.ReleaseNotes):
        model: ModelSettings = FakeSettings(key="notes")

    monkeypatch.setattr(example, "Summariser", FakeSummariser)  # the job looks the agents up when it runs
    monkeypatch.setattr(example, "ReleaseNotes", FakeWriter)
    summaries = Reactive(respond=lambda text, n: structured_reply("SummaryOutput", {"summary": "A change."}))
    notes = Scripted(script=[structured_reply("NotesOutput", {"notes": "The notes."})])
    use_model(summaries, "summaries")
    use_model(notes, "notes")
    runner = AgentRunner(db, retry_delay_s=0)
    commits = example.COMMITS

    first = await run_job(runner, example.release_notes, example.Commits(commits=commits, crash=True), resumable=True)
    assert first.status == Status.FAILED and "simulated crash" in (first.error or "")
    assert [r.agent_name for r in await db.list_attempts(job_id=first.job_id)] == ["summariser"] * len(commits)

    calls_before = sum(summaries.counts.values())
    second = await run_job(runner, example.release_notes, example.Commits(commits=commits), resume_from=first.job_id)
    assert second.status == Status.SUCCESS and second.result.notes == "The notes."
    assert sum(summaries.counts.values()) == calls_before  # the summaries were not asked for again
    assert notes.calls == 1
    rows = await db.list_attempts(job_id=second.job_id)
    reused = [r for r in rows if r.reused_from_attempt_id]
    assert [r.agent_name for r in reused] == ["summariser"] * len(commits)
    assert all((r.input_tokens, r.output_tokens) == (0, 0) for r in reused)
    assert [r.agent_name for r in rows if not r.reused_from_attempt_id] == ["release_notes"]


# the chat examples: a CLI and a FastAPI app over one streaming agent. The model is a fake, and nothing leaves the process.


def load_chat(monkeypatch, name: str):
    """An example of examples/chat, whose files import each other (`from support import Support`), with the agent on a fake."""
    from fakes import FakeSettings

    monkeypatch.syspath_prepend(str(EXAMPLES / "chat"))
    support = __import__("support")

    class Fake(support.Support):
        model: ModelSettings = FakeSettings()

    monkeypatch.setattr(support, "Support", Fake)
    module = __import__(name)
    monkeypatch.setattr(module, "Support", Fake)
    return module


def test_the_chat_agent_is_a_streaming_support_agent_on_the_smallest_claude_model(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLES / "chat"))
    import support

    agent = support.Support()
    assert agent.streaming and [t.name for t in agent.tools] == ["lookup_order"]
    assert (agent.model.model, agent.model.thinking) == ("claude-haiku-5-5", "off")
    assert support.lookup_order.invoke({"order_id": "1001"}).startswith("a blue kettle")
    assert support.lookup_order.invoke({"order_id": "9"}) == "There is no such order."


async def test_the_cli_streams_the_answer_and_remembers_the_conversation(db, monkeypatch, capsys):
    from fakes import Streams, runner_with, tool_call_chunk

    cli = load_chat(monkeypatch, "cli")
    model = Streams(
        turns=[
            [tool_call_chunk("lookup_order", {"order_id": "1001"}, "c1")],
            ["It arrives ", "Thursday."],
            ["Glad to help."],
        ]
    )
    runner_with(db, model)  # the fake model is registered; the CLI builds its own runner on the same file
    monkeypatch.setenv("FTW_DB_PATH", str(db.path))
    lines = iter(["where is order 1001?", "thanks", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    await cli.main()

    out = capsys.readouterr().out
    assert "[lookup_order]" in out and "It arrives Thursday." in out and "Glad to help." in out
    assert [m.content for m in model.seen[2] if m.type == "human"] == [
        "where is order 1001?",
        "thanks",
    ]  # turn two had turn one


async def test_ctrl_c_stops_the_answer_and_the_next_turn_has_what_was_said(db, monkeypatch, capsys):
    import os
    import signal

    from fakes import Hold, Streams, runner_with

    cli = load_chat(monkeypatch, "cli")
    gate = Hold()
    model = Streams(turns=[["Let me check ", gate, "the whole order."], ["Understood."]])
    runner_with(db, model)
    monkeypatch.setenv("FTW_DB_PATH", str(db.path))
    lines = iter(["where is my order?", "no, order 1002", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    loop = asyncio.get_running_loop()

    async def press_ctrl_c():
        await gate.reached.wait()
        await asyncio.sleep(0.03)
        os.kill(os.getpid(), signal.SIGINT)

    task = asyncio.create_task(press_ctrl_c())
    await cli.main()
    await task
    assert loop.is_running()

    out = capsys.readouterr().out
    assert "Let me check " in out and "the whole order." not in out and "Understood." in out
    second = model.seen[1]
    assert [m.content for m in second if m.type in ("human", "ai")] == [
        "where is my order?",
        "Let me check ",
        "no, order 1002",
    ]


async def chat_api(db, monkeypatch, model):
    import httpx
    from fakes import runner_with

    api = load_chat(monkeypatch, "api")
    api.app.state.runner = runner_with(db, model)  # the lifespan opens the real one; a test sets its own
    return api, httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test")


def sse(text: str) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in text.split("\n\n") if line.startswith("data: {")]


async def test_the_api_streams_ag_ui_and_vercel(db, monkeypatch):
    from fakes import Streams

    _, http = await chat_api(db, monkeypatch, Streams(turns=[["Hello ", "there."], ["Hi again."]]))
    async with http:
        body = {"threadId": "t", "runId": "r", "messages": [{"id": "1", "role": "user", "content": "hi"}]}
        response = await http.post("/agui", json=body)
        events = sse(response.text)
        assert response.headers["content-type"].startswith("text/event-stream")
        assert events[0]["type"] == "RUN_STARTED" and events[-1]["type"] == "RUN_FINISHED"
        assert "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT") == "Hello there."

        ui = {"id": "c", "messages": [{"id": "u", "role": "user", "parts": [{"type": "text", "text": "hi"}]}]}
        response = await http.post("/vercel", json=ui)
        assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
        assert sse(response.text)[-1]["type"] == "finish" and response.text.endswith("data: [DONE]\n\n")


async def test_the_api_answers_a_bad_request_with_a_422_and_an_unknown_run_with_a_404(db, monkeypatch):
    from fakes import Streams

    _, http = await chat_api(db, monkeypatch, Streams(turns=[]))
    async with http:
        assert (await http.post("/agui", content="not json")).status_code == 422
        assert (await http.post("/vercel", json={"messages": []})).status_code == 422
        assert (await http.post("/stop/nothing")).status_code == 404


async def test_the_api_stops_a_response(db, monkeypatch):
    from fakes import Hold, Streams

    gate = Hold()
    _, http = await chat_api(db, monkeypatch, Streams(turns=[["Let me check ", gate, "the whole order."]]))
    async with http:
        body = {"threadId": "t", "runId": "r", "messages": [{"id": "1", "role": "user", "content": "where?"}]}
        request = asyncio.create_task(http.post("/agui", json=body))
        await gate.reached.wait()
        await asyncio.sleep(0.03)
        (row,) = await db.list_attempts()
        assert (await http.post(f"/stop/{row.run_id}")).json() == {"stopped": True}
        events = sse((await request).text)
    assert events[-1]["outcome"]["type"] == "cancelled"
    assert (await db.get_attempt(row.attempt_id)).status is Status.CANCELLED


# the result store example: no model, no key


async def test_the_results_example_store_follows_the_contract():
    example = load("results", "store.py")
    await example.main()  # raises AssertionError if the store breaks a rule


async def test_the_contract_catches_a_store_of_the_example_that_forgets_to_delete():
    example = load("results", "store.py")

    class Forgetful(example.FileResultStore):
        async def delete_job(self, job_id: str) -> None:
            return None

    with pytest.raises(AssertionError, match="delete_job must remove every result"):
        await example.testing.check_result_store(lambda: Forgetful(Path(tempfile.mkdtemp())))


# the jobs example: plain code, no model, no key


def jobs_example(monkeypatch, name: str):
    monkeypatch.syspath_prepend(str(EXAMPLES / "jobs"))
    monkeypatch.delitem(__import__("sys").modules, name, raising=False)
    return __import__(name)


async def test_the_jobs_example_submits_a_job_and_a_worker_runs_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "jobs.db"))
    await jobs_example(monkeypatch, "run").main()
    out = capsys.readouterr().out.split()
    assert out[:3] == ["queued", "running", "succeeded"] and "HELLO" in " ".join(out)


async def test_the_jobs_example_api_submits_follows_over_sse_and_reads_the_result(db, monkeypatch):
    import httpx

    shout = jobs_example(monkeypatch, "shout")
    api = jobs_example(monkeypatch, "api")
    queue = SqliteQueue(db)
    api.app.state.client = JobClient(db, queue)  # the lifespan opens its own; a test shares the database
    worker = JobWorker(db, queue, AgentRunner(db), jobs=[shout.shout])
    running = asyncio.create_task(worker.run())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as http:
            job_id = (await http.post("/jobs", json={"text": "hello"})).json()["job_id"]
            stream = await http.get(f"/jobs/{job_id}/events")
            assert stream.headers["content-type"].startswith("text/event-stream")
            assert [line for line in stream.text.splitlines() if line.startswith("event:")] == [
                "event: queued",
                "event: running",
                "event: succeeded",
            ]
            assert (await http.get(f"/jobs/{job_id}/result")).json() == {"shouted": "HELLO"}
            resumed = await http.get(f"/jobs/{job_id}/events", headers={"last-event-id": "0"})
            assert resumed.text.count("event:") == 3
    finally:
        await worker.stop(grace=1)
        await asyncio.wait_for(running, timeout=5)


def test_the_observability_example_prints_the_job_span_and_the_users_span_under_it():
    import subprocess
    import sys

    run = subprocess.run(
        [sys.executable, str(EXAMPLES / "observability" / "trace.py")], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    assert "shouted='HELLO'" in run.stdout
    decoder, spans, at = json.JSONDecoder(), {}, 0
    while (at := run.stdout.find("{\n", at)) != -1:  # the console exporter prints one JSON object for each span
        span, at = decoder.raw_decode(run.stdout, at)
        spans[span["name"]] = span
    job_span, own_span = spans["invoke_workflow shout"], spans["make it loud"]
    assert job_span["parent_id"] is None
    assert own_span["parent_id"] == job_span["context"]["span_id"]


def test_the_takeover_example_kills_a_worker_and_another_takes_the_job_over():
    import subprocess
    import sys

    run = subprocess.run(
        [sys.executable, str(EXAMPLES / "jobs" / "takeover.py")], capture_output=True, text=True, timeout=90
    )
    assert run.returncode == 0, run.stderr
    events = [line for line in run.stdout.splitlines() if line in {"queued", "running", "lost", "succeeded", "failed"}]
    assert events == ["queued", "running", "lost", "running", "succeeded"]
    assert "2 executions" in run.stdout


# the background example: a chat agent that starts a researcher, and a session that wakes it when the researcher is done


async def test_the_background_example_wakes_the_chat_with_the_result_when_the_researcher_is_done(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(EXAMPLES / "background"))
    session = __import__("session")

    class FakeResearcher(session.Researcher):
        model: ModelSettings = FakeSettings(key="researcher")

    class FakeAssistant(session.Assistant):
        model: ModelSettings = FakeSettings(key="assistant")
        background_jobs: list[type] = [FakeResearcher]

    monkeypatch.setattr(session, "Assistant", FakeAssistant)
    start = tool_call_chunk(
        "start_background", {"agent_name": "researcher", "input_json": '{"topic": "kettles"}'}, "t1"
    )
    assistant = Streams(turns=[[start], ["On it, I am researching."], ["The note says: kettles boil water."]])
    use_model(assistant, "assistant")
    use_model(Scripted(script=[structured_reply("Note", {"note": "Kettles boil water."})]), "researcher")

    async with SqliteDatabase(":memory:") as db, AgentRunner(db) as runner:
        chat = session.Session(db, runner)
        await chat.say(HumanMessage("research kettles"))  # the first turn ends with the researcher still working
        await asyncio.wait_for(asyncio.gather(*chat.watching), timeout=30)  # the session wakes the chat on its own

    assert assistant.calls == 3  # the tool call, the answer, and the turn that nobody typed
    woken = assistant.seen[-1]  # what the model was shown in the turn that nobody typed
    assert any("<background_result" in str(m.content) and "Kettles boil water." in str(m.content) for m in woken)
    out = capsys.readouterr().out
    assert "On it, I am researching." in out and "The note says: kettles boil water." in out


async def test_the_attach_example_sends_the_file_as_inline_data_and_prints_the_answer(
    db, monkeypatch, capsys, tmp_path
):
    import base64

    from fakes import FakeSettings, Streams, runner_with

    monkeypatch.syspath_prepend(str(EXAMPLES / "chat"))
    attach = __import__("attach")

    class Fake(attach.Reader):
        model: ModelSettings = FakeSettings()

    monkeypatch.setattr(attach, "Reader", Fake)
    model = Streams(turns=[["A tiny ", "picture."]])
    runner_with(db, model)
    monkeypatch.setenv("FTW_DB_PATH", str(db.path))
    picture = tmp_path / "cat.png"
    picture.write_bytes(b"\x89PNG fake")

    await attach.main(picture, "What is this?")

    assert "A tiny picture." in capsys.readouterr().out
    [human] = [m for m in model.seen[0] if m.type == "human"]
    assert human.content == [
        {"type": "text", "text": "What is this?"},
        {"type": "image", "base64": base64.b64encode(b"\x89PNG fake").decode(), "mime_type": "image/png"},
    ]
    assert Fake.max_attachments == 1


def test_the_attach_example_sends_a_pdf_as_a_document_with_its_filename(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(EXAMPLES / "chat"))
    attach = __import__("attach")
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7")

    part = attach.attachment(pdf)

    assert (part.type, part.source.mime_type, part.metadata) == (
        "document",
        "application/pdf",
        {"filename": "report.pdf"},
    )


def test_the_attach_example_explains_itself_when_the_file_is_missing_or_the_arguments_are_empty(monkeypatch, tmp_path):
    import runpy

    for argv in (
        ["attach.py"],
        ["attach.py", "", ""],
        ["attach.py", str(tmp_path), "what?"],
        ["attach.py", "nope.png", "what?"],
    ):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as exit_:
            runpy.run_path(str(EXAMPLES / "chat" / "attach.py"), run_name="__main__")
        assert "usage: python examples/chat/attach.py" in str(exit_.value)


# the examples that call Gemini, run on a fake model: what the script does with the agent's answer


def restore_the_runner_log(monkeypatch) -> None:
    """The skills example adds a handler to the runner's logger and sets its level: put both back after the test."""
    logger = logging.getLogger("featherweight.runner")
    monkeypatch.setattr(logger, "handlers", list(logger.handlers))
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "_cache", {})


async def test_the_skills_example_reads_a_skill_and_the_file_it_points_to_and_prints_what_it_read(
    tmp_path, monkeypatch, capsys
):
    from fakes import tool_reply

    from featherweight import Gemini

    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "skills.db"))
    example = load("skills")
    restore_the_runner_log(monkeypatch)
    model = Scripted(
        script=[
            tool_reply("get_skill", {"name": "meeting-notes"}, "s1"),
            tool_reply("get_skill", {"name": "meeting-notes", "resource": "references/tags.md"}, "s2"),
            structured_reply("AssistantOutput", {"answer": "[DECISION] Launch on May 19."}),
        ]
    )
    monkeypatch.setattr(Gemini, "build", lambda self: model)

    assert await example.main(["Write notes for this meeting."]) == 0

    out = capsys.readouterr().out
    assert "[read] meeting-notes: instructions" in out and "[read] meeting-notes: references/tags.md" in out
    assert "[DECISION] Launch on May 19." in out
    told = [str(m.content) for m in model.seen[-1] if m.type == "tool"]
    assert "[DECISION]" in told[1]  # the tags are only in the referenced file, and the model was given it


async def test_the_launching_example_launches_both_agents_and_prints_the_runs_as_a_tree(tmp_path, monkeypatch, capsys):
    from fakes import tool_reply

    from featherweight import Gemini

    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "launching.db"))
    example = load("launching")

    def launch(name: str, payload: dict, call_id: str):
        return tool_reply("launch_agent", {"agent_name": name, "input_json": json.dumps(payload)}, call_id)

    model = Scripted(
        script=[
            launch("summariser", {"text": "the article", "max_words": 20}, "l1"),
            structured_reply("SummariserOutput", {"summary": "A warehouse is approved."}),
            launch("translator", {"text": "A warehouse is approved.", "language": "French"}, "l2"),
            structured_reply("TranslatorOutput", {"translation": "Un entrepot est approuve."}),
            structured_reply("EditorOutput", {"headline": "Entrepot", "summary": "Un entrepot est approuve."}),
        ]
    )
    monkeypatch.setattr(Gemini, "build", lambda self: model)

    assert await example.main(["A headline and a summary, in French."]) == 0

    told = [str(m.content) for m in model.seen[-1] if m.type == "tool"]
    assert "A warehouse is approved." in told[0] and "Un entrepot est approuve." in told[1]
    tree = capsys.readouterr().out.split("runs of this job:\n")[1].splitlines()
    assert [(len(line) - len(line.lstrip()), line.split()[0]) for line in tree if line.strip()] == [
        (2, "editor"),
        (4, "summariser"),
        (4, "translator"),
    ]


async def test_the_skills_example_exits_with_a_failure_when_a_run_fails(tmp_path, monkeypatch, capsys):
    from featherweight import Gemini

    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "failed.db"))
    example = load("skills")
    restore_the_runner_log(monkeypatch)
    model = Scripted(script=[RuntimeError("down")] * 3)
    monkeypatch.setattr(Gemini, "build", lambda self: model)

    assert await example.main(["anything"]) == 1
    assert "failed:" in capsys.readouterr().out


async def test_the_mcp_example_binds_only_its_allow_list_and_prints_the_answer_with_its_sources(
    tmp_path, monkeypatch, capsys
):
    from fakes import tool_reply
    from mcp_helpers import fake as fake_server
    from mcp_helpers import use_real_stderr

    from featherweight import Gemini

    use_real_stderr(monkeypatch)
    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "mcp.db"))
    example = load("mcp")

    class LocalReader(example.RepoReader):  # the same agent, with the tests' server in place of GitHub's
        mcp_servers: list = [fake_server(name="github", tools=("add",))]

    monkeypatch.setattr(example, "RepoReader", LocalReader)
    model = Scripted(
        script=[
            tool_reply("github_add", {"a": 2, "b": 3}, "t1"),
            structured_reply("AnswerOutput", {"answer": "Five.", "sources": ["https://github.com/a/b"]}),
        ]
    )
    monkeypatch.setattr(Gemini, "build", lambda self: model)

    assert await example.main(["What is two and three?"]) == 0

    out = capsys.readouterr().out
    assert "Five." in out and "https://github.com/a/b" in out and "tokens" not in out and "in /" in out
    assert "github_add" in model.bound[0] and "github_pid" not in model.bound[0]
    [told] = [str(m.content) for m in model.seen[-1] if m.type == "tool"]
    assert "5" in told
