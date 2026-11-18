"""The one-command CLI with a mocked LLM: learn, show the overview, fill the form (manual or auto), generate, save the
run folder. The agents are the ones of test_pipeline.py; the terminal (questions and editor) is scripted."""

import json
import logging

import pytest

from fakes import Reactive, structured_reply, tool_reply
from doclearn import cli
from doclearn.assembly import derive_facts_schema
from doclearn.models import Condition, FieldInfo, LearnedNode, LearnedTree
from doclearn.report import GUIDANCE_KEY, form_file_content
from micro.runner import AgentRunner
from builders import make_respond

DOCUMENT = "# PROPOSAL\n\n**Proposal No.:** PRP-1\n"
GENERATED = "# PROPOSAL\n**Proposal No.:** PRP-9\n## CLIENT\n**Name:** Alder Ltd\n## PRICING\n- Total price: $5.\n"
FACTS = {"proposal_no": "PRP-9", "client_name": "Alder Ltd", "total_price": "$5"}
STORY = "Alder Ltd asks for a robotics integration."


class Session:
    """The mocked model, what the Generator and the Form Filler were sent, and a scripted terminal."""

    def __init__(self, answers=(), edits=()):
        self.answers, self.edits = list(answers), list(edits)
        self.questions: list[str] = []
        self.generator_inputs: list[dict] = []
        self.filler_calls = 0
        self.edited = 0

    def respond(self, user_text, call_number):
        payload = json.loads(user_text)
        if "structure" in payload and "facts" in payload:  # the Generator
            total = {f["name"]: f["value"] for f in payload["facts"]}["total_price"]  # the document follows the facts
            if call_number == 1:  # the document is handed in through the tool, then the agent returns its notes
                self.generator_inputs.append(payload)
                return tool_reply("check_document", {"markdown": GENERATED.replace("$5", total)}, "t1")
            return structured_reply("GeneratorOutput", {"notes": ["used the short wording"]})
        if "structure" in payload:  # the Form Filler
            self.filler_calls += 1
            facts = [{"name": n, "value": v} for n, v in FACTS.items()]
            return structured_reply("FormFillerOutput", {"facts": facts, "story": STORY})
        return make_respond()(user_text, call_number)

    def ask(self, prompt):
        self.questions.append(prompt)
        return self.answers.pop(0)

    def edit(self, path):
        """The scripted user: each edit is a function of the file's current text, or the text to write."""
        self.edited += 1
        step = self.edits.pop(0)
        path.write_text(step(path.read_text()) if callable(step) else step, encoding="utf-8")

    def console(self):
        return cli.Console(ask=self.ask, edit=self.edit)


def form_with(facts=FACTS, story="A manual story."):
    return json.dumps({**facts, GUIDANCE_KEY: story})


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    folder = tmp_path / "docs"
    folder.mkdir()
    for name in ("a", "b", "c"):
        (folder / f"{name}.md").write_text(DOCUMENT)
    monkeypatch.setenv("MICRO_DB_PATH", str(tmp_path / "cli.db"))
    yield tmp_path
    for name in ("micro", "doclearn"):  # `main` attaches handlers bound to this test's stdout; make sure none are left
        logger = logging.getLogger(name)
        assert not [h for h in logger.handlers if getattr(h, cli.LOG_HANDLER_FLAG, False)]


def invoke(workdir, session, monkeypatch, *args):
    model = Reactive(respond=session.respond)
    monkeypatch.setattr(cli, "AgentRunner", lambda db: AgentRunner(db, model_factory=lambda agent: model, retry_delay_s=0.0))
    argv = [str(workdir / "docs"), "--category", "proposals", "--out-dir", str(workdir / "runs"), *args]
    return cli.main(argv, session.console())


def run_dir(workdir):
    (folder,) = (workdir / "runs").iterdir()
    return folder


def test_manual_fill_opens_the_form_in_the_editor_generates_with_its_story_and_saves_the_run_folder(workdir, monkeypatch, capsys):
    shown = {}

    def edit_form(text):
        shown["form"] = json.loads(text)
        return form_with()

    session = Session(edits=[edit_form])
    code = invoke(workdir, session, monkeypatch, "--fill", "manual")
    out = capsys.readouterr().out

    assert code == cli.DONE and session.edited == 1 and session.questions == []
    assert [k for k in shown["form"] if not k.startswith("_")] == ["proposal_no", "client_name", "total_price", "include_training"]
    assert session.generator_inputs[0]["guidance"] == "A manual story."
    assert {f["name"]: f["value"] for f in session.generator_inputs[0]["facts"]} == FACTS
    assert "used the short wording" in out and "include_training" in out  # the optional input with no value

    folder = run_dir(workdir)
    assert (folder / "generated.md").read_text() == GENERATED
    assert (folder / "story.txt").read_text().strip() == "A manual story."
    assert json.loads((folder / "facts.json").read_text())["proposal_no"] == "PRP-9"
    assert {p.name for p in (folder / "learned").iterdir()} == {
        "tree.json", "template.json", "facts_schema.json", "conformance.json", "outline.txt", "overview.md"
    }
    assert [n["text"] for n in json.loads((folder / "learned/tree.json").read_text())["nodes"]] == ["# PROPOSAL", "## CLIENT", "## PRICING"]
    assert [c["document_id"] for c in json.loads((folder / "learned/conformance.json").read_text())["conformance"]] == ["a", "b", "c"]
    assert "Conformance of the source documents" in (folder / "learned/overview.md").read_text()
    assert "Tree: 3 units" in (folder / "summary.md").read_text()  # the only guard for the summary's overview wording


@pytest.mark.parametrize("verbose", [False, True])
def test_the_terminal_shows_progress_and_the_overview_and_log_lines_only_with_verbose(workdir, monkeypatch, capsys, verbose):
    invoke(workdir, Session(edits=[form_with()]), monkeypatch, "--fill", "manual", *(["-v"] if verbose else []))
    out = capsys.readouterr().out

    for line in ("Analyzing 3 documents", "Reconciling", "Aligning", "Assembling", "Writing the document"):
        assert line in out
    assert "Tree: 3 units" in out and "Template: 4 element types" in out
    assert "Required (3): proposal_no (text)" in out and "Optional (1): include_training (boolean)" in out
    assert "a: 100% (" in out  # the conformance of each source document
    assert (" INFO " in out) == verbose and ("agent_run" in out) == verbose
    log = (run_dir(workdir) / "run.log").read_text()  # the log file always has them
    assert "learning_finished" in log and "generation_finished" in log


def change(text):
    form = json.loads(text)
    form["total_price"] = "$7"
    form[GUIDANCE_KEY] = "Edited story."
    return json.dumps(form)


@pytest.mark.parametrize(
    "answers, edits, code, filler_calls, guidance, total",
    [
        (["g"], [], cli.DONE, 1, STORY, "$5"),
        (["r", "g"], [], cli.DONE, 2, STORY, "$5"),  # redone before generating
        (["e", "g"], [change], cli.DONE, 1, "Edited story.", "$7"),  # edited before generating
        (["q"], [], cli.STOPPED, 1, None, None),
    ],
)
def test_the_auto_filled_case_is_shown_and_can_be_generated_redone_edited_or_quit(
    workdir, monkeypatch, capsys, answers, edits, code, filler_calls, guidance, total
):
    session = Session(answers=answers, edits=edits)
    assert invoke(workdir, session, monkeypatch, "--fill", "auto") == code
    out = capsys.readouterr().out

    assert session.filler_calls == filler_calls and session.edited == len(edits)
    assert "Inventing a case" in out and "proposal_no: PRP-9" in out and STORY in out
    if guidance is None:
        assert session.generator_inputs == [] and not (run_dir(workdir) / "generated.md").exists()
        assert "Stopped before generating" in out
    else:
        (sent,) = session.generator_inputs
        assert sent["guidance"] == guidance and {f["name"]: f["value"] for f in sent["facts"]}["total_price"] == total
        assert (run_dir(workdir) / "story.txt").read_text().strip() == guidance


@pytest.mark.parametrize(
    "answers, edits, code, questions",
    [
        (["nonsense", "q"], [], cli.STOPPED, 2),  # not understood, asked again; quitting keeps what was learned
        (["manual"], [form_with()], cli.DONE, 1),  # the mode can be chosen by its first word
    ],
)
def test_without_fill_it_asks_for_the_mode(workdir, monkeypatch, answers, edits, code, questions):
    session = Session(answers=answers, edits=edits)
    assert invoke(workdir, session, monkeypatch) == code
    assert len(session.questions) == questions and "[m]anual" in session.questions[0] and "[a]uto" in session.questions[0]
    assert (run_dir(workdir) / "learned" / "tree.json").exists()


@pytest.mark.parametrize(
    "answers, edits, code, edited, messages",
    [
        # bad JSON, answer y; then missing inputs and a bad number, answer y again
        (["y", "y"], ["{not json", form_with({"proposal_no": "PRP-9", "total_price": "lots"}), form_with()], cli.DONE, 3,
         ["is not valid JSON", "required fields without a value: ['client_name']", "'lots' is not a valid number"]),
        (["n"], ["{not json"], cli.STOPPED, 1, ["is not valid JSON"]),  # refusing to edit again stops
    ],
)
def test_a_form_with_problems_goes_back_to_the_editor_until_it_is_complete_or_the_user_stops(
    workdir, monkeypatch, capsys, answers, edits, code, edited, messages
):
    session = Session(answers=answers, edits=edits)
    assert invoke(workdir, session, monkeypatch, "--fill", "manual") == code
    out = capsys.readouterr().out
    assert session.edited == edited and all(m in out for m in messages)
    assert bool(session.generator_inputs) == (code == cli.DONE)


def test_problems_the_user_can_fix_exit_with_1_and_a_message(workdir, monkeypatch, capsys):
    (workdir / "empty").mkdir()
    code = cli.main([str(workdir / "empty"), "--category", "proposals", "--out-dir", str(workdir / "runs")], Session().console())
    assert code == cli.FAILED and "No .md documents" in capsys.readouterr().err


def test_the_form_file_lists_the_required_inputs_first_with_their_notes_and_is_read_back(tmp_path):
    tree = LearnedTree(
        category="c",
        nodes=[
            LearnedNode(type="t", text="a {{y}}", kind="slot", alternatives=["a"],
                        children=[LearnedNode(type="t", text="b {{z}}", kind="slot", conditions=[Condition(field="f", op="=", value="true")])]),
            LearnedNode(type="t", text="c {{x}}", kind="slot"),
        ],
        fields=[FieldInfo(name=n, type="number" if n == "x" else "text", description=f"the {n}") for n in "yzfx"],
    )
    schema = derive_facts_schema(tree)
    path = tmp_path / "form.json"
    path.write_text(form_file_content(schema, {"y": "1"}, "story"))

    raw = json.loads(path.read_text())
    assert [k for k in raw if not k.startswith("_")] == ["x", "y", "z", "f"]  # x is required: first
    assert raw["_x"] == "REQUIRED, number: the x" and raw["_y"].startswith("optional, text")
    assert raw[GUIDANCE_KEY] == "story"
    facts, story = cli.read_form(path)
    assert facts == {"y": "1"} and story == "story"  # empty values are left out
    assert cli.form_problems(schema, facts) == ["required fields without a value: ['x']"]
    path.write_text("[1, 2]")
    with pytest.raises(cli.CliError, match="JSON object"):
        cli.read_form(path)
