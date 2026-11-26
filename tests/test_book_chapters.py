"""Every chapter of the book that has code, and the finished project, run as the reader runs them. Only the model is a fake:
the MCP server, the databases, the queue and the worker processes are real. Chapters 4-4, 5-5 and 5-6 are in
`test_book_code.py`. A new chapter folder that no test names fails here."""

import asyncio
import importlib.util
import json
import os
import re
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fakes import Hold, Streams, structured_reply, tool_call_chunk, tool_reply
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from mcp_helpers import use_real_stderr

from featherweight import AgentRunner, Claude, Gemini, SqliteDatabase

ROOT = Path(__file__).parent.parent
CODE = ROOT / "docs" / "book" / "code"
REPORTS = sorted((CODE / "reports").glob("*.txt"))
TITLES = {path.name: f"Title of {path.name[:2]}" for path in REPORTS}
SEVERITY = {"01": "low", "02": None, "03": "high", "04": "medium"}  # so that sorting has something to do


class Model(BaseChatModel):
    """A fake model for every agent of a chapter: `reply(system, messages)` gives each answer. `seen` keeps every call."""

    reply: Any
    seen: list = []
    bound: list = []

    @property
    def _llm_type(self) -> str:
        return "book"

    def bind_tools(self, tools, **kwargs):
        self.bound.append([getattr(t, "name", str(t)) for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        item = self.reply(text_of(messages, "system"), messages)
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


def text_of(messages, kind: str) -> str:
    return " ".join(str(m.content) for m in messages if m.type == kind)


def script(*items) -> Model:
    left = list(items)
    return Model(reply=lambda system, messages: left.pop(0))


def report_number(messages) -> str:
    """Which sample report a run was given."""
    given = json.loads([m.content for m in messages if m.type == "human"][0])["text"]
    return next(path.name[:2] for path in REPORTS if path.read_text() == given)


def summary_of(messages, **extra):
    number = report_number(messages)
    return structured_reply("Summary", {"title": f"Title of {number}", "severity": SEVERITY[number], **extra})


def summariser() -> Model:
    return Model(reply=lambda system, messages: summary_of(messages))


@pytest.fixture
def chapter(monkeypatch, tmp_path):
    """Load a file of a chapter as the reader runs it: from the book's code folder, with its own folder importable."""
    local = {path.stem for path in CODE.rglob("*.py")}

    def forget():
        for name in local & set(sys.modules):
            if str(getattr(sys.modules[name], "__file__", "")).startswith(str(CODE)):
                del sys.modules[name]

    use_real_stderr(monkeypatch)
    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "book.db"))
    monkeypatch.chdir(CODE)

    def load(folder: str, file: str = "app", model: BaseChatModel | None = None):
        forget()
        if model is not None:
            monkeypatch.setattr(Gemini, "build", lambda self: model)
        monkeypatch.syspath_prepend(str(CODE / folder))
        spec = importlib.util.spec_from_file_location(f"book_{folder}_{file}", CODE / folder / f"{file}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    yield load
    forget()


def lines_typed(monkeypatch, module, *lines: str) -> None:
    """What the reader types at the prompt of a chat of the book, then an empty line to leave."""
    left = [*lines, ""]

    async def read_line(prompt: str) -> str:
        return left.pop(0)

    monkeypatch.setattr(module, "read_line", read_line)


def the_input(messages) -> dict:
    return json.loads([m.content for m in messages if m.type == "human"][0])


def tool_results(model: Model) -> dict[str, str]:
    """What each tool call gave back, by the id of the call."""
    return {m.tool_call_id: str(m.content) for messages in model.seen for m in messages if m.type == "tool"}


# Part 1


def test_1_1_says_which_keys_are_set(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")
    runpy.run_path(str(CODE / "1-1" / "app.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY is set" in out and "GOOGLE_API_KEY is not set" in out and "dummy-key" not in out


def test_1_2_builds_the_agent_without_a_model(capsys):
    runpy.run_path(str(CODE / "1-2" / "app.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert out.startswith("incident_summariser 1")
    assert "'severity'" in out and "'open_questions'" in out


@pytest.mark.parametrize("folder", ["1-3", "1-4", "1-6"])
async def test_part_1_runs_the_summariser_on_the_report_and_prints_the_result(chapter, capsys, folder):
    model = summariser()
    app = chapter(folder, model=model)
    await app.main()
    out = capsys.readouterr().out
    assert "SUCCESS" in out and "Title of 01" in out
    assert the_input(model.seen[0]) == {"text": REPORTS[0].read_text()}
    if folder == "1-4":
        assert "attempt 1: SUCCESS, gemini-3.5-flash-lite, tokens=5+7" in out and "input_hash=" in out


async def test_1_5_rejects_a_service_the_report_does_not_name_and_retries(chapter, capsys, monkeypatch):
    model = Model(reply=None)
    replies = [{"service": "Billing"}, {"title": "Sign-in trouble"}]
    model.reply = lambda system, messages: structured_reply("Summary", replies.pop(0))
    app = chapter("1-5", model=model)
    await app.main()
    out = capsys.readouterr().out
    assert "SUCCESS attempts=2" in out
    assert "attempt 1: FAILED ValueError" in out and "attempt 2: SUCCESS" in out


# Part 2: each chapter adds an ability, and the next keeps it


@pytest.mark.parametrize("folder", ["2-1", "2-2", "2-3", "2-4"])
async def test_part_2_gives_the_agent_each_ability_and_its_answer_comes_back(chapter, capsys, folder):
    level = int(folder[-1])
    calls = [tool_reply("minutes_between", {"start": "2026-11-20 10:00", "end": "2026-11-20 11:30"}, "time")]
    if level >= 2:
        calls.append(tool_reply("search_tools", {"keywords": ["owns", "service"]}, "find"))
        calls.append(
            tool_reply("call_tool", {"name": "services_owner_of", "args_json": '{"service": "search"}'}, "own")
        )
    if level >= 3:
        calls.append(tool_reply("get_skill", {"name": "severity-rubric"}, "skill"))
    if level >= 4:
        launch = {"agent_name": "action_planner", "input_json": '{"impact": "slow sign-in"}'}
        calls.append(tool_reply("launch_agent", launch, "plan"))
    summary = structured_reply("Summary", {"title": "Done"})
    left = list(calls)

    def reply(system, messages):
        if "Propose two or three" in system:
            return structured_reply("Plan", {"steps": ["Add an alert"]})
        return left.pop(0) if left else summary

    model = Model(reply=reply)
    app = chapter(folder, model=model)
    await app.main()

    assert "SUCCESS" in capsys.readouterr().out
    results = tool_results(model)
    assert results["time"] == "90"
    if level >= 2:
        assert "services_owner_of" in results["find"]
        assert "discovery team, on call: Ben" in results["own"]  # from the real MCP server
    if level >= 3:
        assert "severity" in results["skill"].lower() and "not one of your skills" not in results["skill"]
    if level >= 4:
        assert "Add an alert" in results["plan"]


# Part 3: jobs


async def test_3_1_runs_one_report_and_then_all_of_them(chapter, capsys):
    app = chapter("3-1", model=summariser())
    await app.main()
    out = capsys.readouterr().out
    assert "SUCCESS Title of 01" in out
    assert "SUCCESS ['Title of 01', 'Title of 02', 'Title of 03', 'Title of 04']" in out  # in the order of the paths


async def test_3_1_fails_the_digest_and_names_the_report_that_failed(chapter, capsys):
    def reply(system, messages):
        return RuntimeError("down") if report_number(messages) == "03" else summary_of(messages)

    app = chapter("3-1", model=Model(reply=reply))
    from featherweight import run_job

    async with SqliteDatabase(":memory:") as db:
        paths = [str(path.relative_to(CODE)) for path in REPORTS]
        outcome = await run_job(AgentRunner(db, retry_delay_s=0), app.summarise_all, app.Reports(paths=paths))
    assert outcome.status.value == "FAILED"
    assert "03-search-latency.txt" in outcome.error and "01-checkout" not in outcome.error


async def test_3_2_shows_the_jobs_status_events_and_attempts(chapter, capsys):
    app = chapter("3-2", model=summariser())
    await app.main()
    out = capsys.readouterr().out
    assert "job: summarise_all SUCCESS" in out and "events: ['queued', 'running', 'succeeded']" in out
    assert out.count("incident_summariser attempt 1: SUCCESS, tokens=5+7") == 4


async def test_3_3_keeps_the_output_in_the_store_that_was_chosen_and_nothing_without_one(chapter, capsys):
    app = chapter("3-3", model=summariser())
    await app.main()
    out = capsys.readouterr().out
    assert "returned: Title of 01" in out and re.search(r"in the store: \{.*'title': 'Title of 01'", out)
    assert "results=None, still returned: True" in out


async def test_3_4_resumes_without_a_model_call_and_sorts_by_severity(chapter, capsys):
    model = summariser()
    app = chapter("3-4", model=model)
    await app.main()
    out = capsys.readouterr().out
    assert "first: FAILED" in out and "simulated crash" in out and "second: SUCCESS" in out
    assert len(model.seen) == 4  # the four summaries of the first job, and none again
    assert out.split("second run")[1].count("reused: no model call") == 4

    from featherweight import run_job

    async with SqliteDatabase(":memory:") as db:
        paths = [str(path.relative_to(CODE)) for path in REPORTS]
        outcome = await run_job(AgentRunner(db), app.summarise_all, app.Reports(paths=paths))
    assert [s.severity for s in outcome.result.summaries] == ["high", "medium", "low", None]


# Part 4: chat


async def test_4_1_streams_the_answers_and_sends_the_whole_conversation(chapter, capsys, monkeypatch):
    model = Streams(turns=[["The checkout ", "was down."], ["For an hour."]])
    app = chapter("4-1")
    monkeypatch.setattr(Gemini, "build", lambda self: model)
    lines_typed(monkeypatch, app, "What broke?", "How long?")
    await app.main()
    out = capsys.readouterr().out
    assert "The checkout was down." in out and "For an hour." in out
    sent = [m.content for m in model.seen[1] if m.type in ("human", "ai")]
    assert sent[0] == REPORTS[0].read_text()
    assert sent[2:] == ["What broke?", "The checkout was down.", "How long?"]


async def test_4_2_streams_both_protocols_and_refuses_a_bad_body_and_too_many_files(chapter, monkeypatch):
    import httpx

    model = Streams(turns=[["Hello."], ["Again."]])
    app = chapter("4-2")
    monkeypatch.setattr(Gemini, "build", lambda self: model)
    async with SqliteDatabase(":memory:") as db, AgentRunner(db) as runner:
        app.app.state.runner = runner
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            agui = {"threadId": "t", "runId": "r", "messages": [{"id": "1", "role": "user", "content": "hi"}]}
            response = await http.post("/agui", json=agui)
            assert "RUN_STARTED" in response.text and "RUN_FINISHED" in response.text and "Hello." in response.text
            ui = {"id": "c", "messages": [{"id": "u", "role": "user", "parts": [{"type": "text", "text": "hi"}]}]}
            response = await http.post("/vercel", json=ui)
            assert response.text.endswith("data: [DONE]\n\n") and "Again." in response.text
            assert (await http.post("/vercel", content="not json")).status_code == 422
            file = {"type": "file", "mediaType": "image/png", "url": "data:image/png;base64,iVBORw0KGgo="}
            many = {"id": "c", "messages": [{"id": "u", "role": "user", "parts": [file] * 50}]}
            assert (await http.post("/vercel", json=many)).status_code == 413


async def test_4_3_keeps_a_stopped_answer_in_its_own_file_and_goes_on_from_it(chapter, capsys, monkeypatch):
    gate = Hold()
    model = Streams(turns=[["Let me check ", gate, "all of it."], ["Understood."]])
    app = chapter("4-3")
    monkeypatch.setattr(Gemini, "build", lambda self: model)
    lines_typed(monkeypatch, app, "What broke?")

    async def press_ctrl_c():
        await gate.reached.wait()
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    pressing = asyncio.create_task(press_ctrl_c())
    await asyncio.wait_for(app.main(), timeout=20)
    await pressing
    assert "all of it." not in capsys.readouterr().out
    assert app.SAVED.exists()

    lines_typed(monkeypatch, app, "Never mind.")  # a second start reads the file
    await app.main()
    sent = [m.content for m in model.seen[1] if m.type in ("human", "ai")]
    assert sent[2:] == ["What broke?", "Let me check ", "Never mind."]


# Part 5: production


async def test_5_1_a_worker_runs_the_submitted_digest(chapter, capsys):
    app = chapter("5-1", model=summariser())
    await asyncio.wait_for(app.main(), timeout=30)
    out = capsys.readouterr().out.splitlines()
    assert out == ["queued", "running", "succeeded", "4 summaries; first: Title of 03"]


async def test_5_1_the_client_and_the_worker_in_their_own_files_meet_in_the_database(chapter, capsys, monkeypatch):
    model = summariser()
    submit = chapter("5-1", "submit", model=model)
    worker = chapter("5-1", "worker", model=model)
    monkeypatch.setitem(sys.modules, "digest", sys.modules["digest"])
    working = asyncio.create_task(worker.main())
    try:
        # the two files loaded their own `digest`: the job is found by its name, as between two processes
        await asyncio.wait_for(submit.main(), timeout=30)
    finally:
        working.cancel()
        await asyncio.gather(working, return_exceptions=True)
    assert "4 summaries; first: Title of 03" in capsys.readouterr().out


def test_5_2_another_worker_takes_over_the_job_of_a_killed_one(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    run = subprocess.run(
        [sys.executable, str(CODE / "5-2" / "app.py")], capture_output=True, text=True, timeout=90, env=env
    )
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert [x for x in lines if x in {"queued", "running", "lost", "succeeded", "failed"}] == [
        "queued",
        "running",
        "lost",
        "running",
        "succeeded",
    ]
    assert "2 executions" in lines


# the background chats of the book, the project and the example

# A known bug, kept visible: remove the mark with the fix (a strict xfail fails when the test starts to pass).
TOLD_TWICE = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="bug: this session wakes the chat for a result that the running turn was already given",
)

AGENT_NAMES = {"5-3": "root_cause_researcher", "project": "root_cause_researcher", "background": "researcher"}
INPUT_FIELDS = {"5-3": "text", "project": "text", "background": "topic"}


RESEARCHERS = ("most likely root cause of the incident", "Write a note of two or three sentences")


class Chat(BaseChatModel):
    """One fake for the chat and for the researcher it starts: the system prompt says which of them is asking."""

    chat: Any
    research: list = []

    @property
    def _llm_type(self) -> str:
        return "chat"

    def bind_tools(self, tools, **kwargs):
        return self

    @staticmethod
    def note(messages) -> tuple[str, dict]:
        """The output of the researcher that is asking: the book's, or the one of the example."""
        if RESEARCHERS[0] in text_of(messages, "system"):
            return "RootCause", {"cause": "A bad release."}
        return "Note", {"note": "A bad release."}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.research.append(messages)
        return ChatResult(generations=[ChatGeneration(message=structured_reply(*self.note(messages)))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        system = text_of(messages, "system")
        if not any(mark in system for mark in RESEARCHERS):
            async for chunk in self.chat._astream(messages):
                yield chunk
            return
        self.research.append(messages)
        call = tool_call_chunk(*self.note(messages), "r1")
        yield ChatGenerationChunk(message=call)


@pytest.mark.parametrize(("folder", "file"), [("5-3", "app"), ("project", "chat")])
async def test_the_chat_is_woken_with_the_note_of_its_background_researcher(chapter, capsys, monkeypatch, folder, file):
    start = tool_call_chunk(
        "start_background",
        {"agent_name": "root_cause_researcher", "input_json": '{"text": "the report"}'},
        "b1",
    )
    streamed = Streams(turns=[[start], ["Researching."], ["The cause was a bad release."]])
    app = chapter(folder, file)
    model = Chat(chat=streamed)
    monkeypatch.setattr(Gemini, "build", lambda self: model)

    async with SqliteDatabase(":memory:") as db, AgentRunner(db) as runner:
        session = app.Session(db, runner, [])
        await session.say(HumanMessage("What was the root cause?"))
        await asyncio.wait_for(asyncio.gather(*session.watching), timeout=30)

    [asked] = model.research  # the researcher ran once
    assert the_input(asked) == {"text": "the report"}
    woken = str([m.content for m in streamed.seen[-1]])
    assert "<background_result" in woken and "A bad release." in woken
    assert "The cause was a bad release." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("folder", "file"),
    [
        pytest.param("5-3", "app", marks=TOLD_TWICE),
        ("project", "chat"),
        pytest.param("background", "session", marks=TOLD_TWICE),
    ],
)
async def test_a_researcher_that_ends_during_the_turn_is_not_told_twice(chapter, monkeypatch, folder, file):
    """The spec: a child that ends while the turn runs is added to that turn. The session must not start another for it."""
    gate = Hold()
    start = tool_call_chunk(
        "start_background",
        {"agent_name": AGENT_NAMES[folder], "input_json": json.dumps({INPUT_FIELDS[folder]: "the report"})},
        "b1",
    )
    # The first call starts the researcher. The second is held while the researcher ends, and asks for one more (unknown)
    # tool, so that the turn has a third call: the one that the result is added to.
    another = tool_call_chunk("no_such_tool", {}, "x1")
    streamed = Streams(
        turns=[[start], [gate, another], ["The cause was a bad release."], ["A turn that must not happen."]]
    )
    model = Chat(chat=streamed)
    if folder == "background":  # the example: its agents name Claude
        monkeypatch.setattr(Claude, "build", lambda self: model)
        monkeypatch.syspath_prepend(str(ROOT / "examples" / "background"))
        spec = importlib.util.spec_from_file_location(
            "example_session", ROOT / "examples" / "background" / "session.py"
        )
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
    else:
        app = chapter(folder, file)
        monkeypatch.setattr(Gemini, "build", lambda self: model)

    async with SqliteDatabase(":memory:") as db, AgentRunner(db) as runner:
        session = app.Session(db, runner) if folder == "background" else app.Session(db, runner, [])
        saying = asyncio.create_task(session.say(HumanMessage("What was the root cause?")))
        await asyncio.wait_for(gate.reached.wait(), timeout=10)
        async with asyncio.timeout(10):  # the researcher ends while the turn is still running
            while not any(
                row.status.value == "SUCCESS" and "researcher" in row.agent_name for row in await db.list_attempts()
            ):
                await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        gate.release.set()
        await asyncio.wait_for(saying, timeout=10)
        await asyncio.wait_for(asyncio.gather(*session.watching), timeout=10)

    assert "<background_result" in str([m.content for m in streamed.seen[2]])  # the turn that was running was given it
    assert streamed.calls == 3, "the session started another turn for a result that the model already had"


def run_as_a_script(tmp_path, path: str) -> str:
    """Run a file of the book in a process of its own, as the reader does, with the summariser's fake as the model. The
    chapters with traces need it: OpenTelemetry takes its global tracer provider once in a process."""
    start = (
        "import runpy, sys\n"
        "from featherweight import Gemini\n"
        "from test_book_chapters import summariser\n"
        "model = summariser()\n"
        "Gemini.build = lambda self: model\n"
        f"sys.path.insert(0, {str((CODE / path).parent)!r})\n"
        f"sys.argv = [{path!r}]\n"
        f"runpy.run_path({path!r}, run_name='__main__')\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")]),
        "FTW_DB_PATH": str(tmp_path / "script.db"),
    }
    run = subprocess.run([sys.executable, "-c", start], cwd=CODE, env=env, capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout


def test_5_4_prints_the_trace_as_a_tree_with_the_readers_own_span(tmp_path):
    lines = run_as_a_script(tmp_path, "5-4/app.py").splitlines()
    assert lines[0].startswith("invoke_workflow")
    assert any(line.startswith("  read the report") for line in lines)
    assert any(line.startswith("  invoke_agent") for line in lines)
    assert any(line.startswith("    chat") for line in lines)


# Part 6: your own storage


@pytest.mark.skipif(not os.environ.get("FTW_TEST_POSTGRES_URL"), reason="needs FTW_TEST_POSTGRES_URL")
async def test_6_1_runs_the_log_in_postgres(chapter, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["FTW_TEST_POSTGRES_URL"])
    await chapter("6-1").main()
    assert capsys.readouterr().out.strip() == "INIT"


@pytest.mark.parametrize(("folder", "says"), [("6-2", "ResultStore contract"), ("6-5", "Observer contract")])
async def test_part_6_the_readers_own_class_passes_the_kit(chapter, capsys, folder, says):
    await chapter(folder).main()
    assert says in capsys.readouterr().out


def test_6_3_the_sketch_has_every_method_a_queue_needs(chapter):
    sketch = chapter("6-3", "sketch")
    sketch.SqsQueue(client=None, url="q")  # a missing abstract method cannot be built


# the finished project


def test_the_finished_project_summarises_every_report_and_counts_its_spans(tmp_path):
    out = run_as_a_script(tmp_path, "project/run.py")
    assert "job succeeded" in out
    rows = [line for line in out.splitlines() if "| " in line]
    titles = [re.search(r"Title of \d\d", row).group() for row in rows]
    assert titles == [f"Title of {n}" for n in ("03", "04", "01", "02")]  # the most severe first
    assert "1 invoke_workflow spans" in out and "4 invoke_agent spans" in out and "4 chat spans" in out


# nothing is left out


def test_every_folder_of_the_books_code_is_run_by_a_test():
    here = Path(__file__).parent
    text = (here / "test_book_chapters.py").read_text() + (here / "test_book_code.py").read_text()
    for folder in sorted(p.name for p in CODE.iterdir() if p.is_dir() and any(p.glob("*.py"))):
        if folder.startswith("2-"):
            named = '["2-1", "2-2", "2-3", "2-4"]' in text
        else:
            named = f'"{folder}"' in text or f'"{folder}/' in text
        assert named, f"no test runs docs/book/code/{folder}"
