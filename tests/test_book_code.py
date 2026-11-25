"""The code of the book's chapters, on a fake model: what a reader types is what runs."""

import importlib.util
from pathlib import Path

from fakes import FakeSettings, Scripted, Streams, runner_with, structured_reply, tool_reply
from langchain_core.messages import ToolMessage

from featherweight.model_settings import ModelSettings

CODE = Path(__file__).parent.parent / "docs" / "book" / "code"


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, CODE / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_chapter_4_4_sends_the_pdf_with_the_question_and_keeps_it_in_the_history(db, monkeypatch, capsys):
    app = load("book_4_4", "4-4/app.py")

    class Fake(app.IncidentAssistant):
        model: ModelSettings = FakeSettings()

    monkeypatch.setattr(app, "IncidentAssistant", Fake)
    model = Streams(turns=[["Roll back ", "the release."], ["The payments team."]])
    runner_with(db, model)
    monkeypatch.setenv("FTW_DB_PATH", str(db.path))

    await app.main(str(CODE / "reports" / "01-checkout-outage.txt"), str(CODE / "4-4" / "runbook.pdf"))

    out = capsys.readouterr().out
    assert "Roll back the release." in out and "The payments team." in out
    [question] = [m for m in model.seen[0] if m.type == "human" and isinstance(m.content, list)]
    text, pdf = question.content
    assert text == {"type": "text", "text": "What does the runbook say to do about this?"}
    assert (pdf["type"], pdf["mime_type"], pdf["filename"]) == ("file", "application/pdf", "runbook.pdf")
    assert pdf["base64"].startswith("JVBERi")  # "%PDF" in base64: the real file, inline
    assert any(
        m.content == question.content for m in model.seen[1] if m.type == "human"
    )  # the file is still in turn two


def test_chapter_4_4_attaches_an_image_as_an_image_block(tmp_path):
    app = load("book_4_4_image", "4-4/app.py")
    picture = tmp_path / "dashboard.png"
    picture.write_bytes(b"\x89PNG fake")

    block = app.attach("What is this?", str(picture)).content[1]

    assert (block["type"], block["mime_type"], block["filename"]) == ("image", "image/png", "dashboard.png")


def test_the_chapter_sets_its_own_attachment_limit():
    assert load("book_4_4_limit", "4-4/app.py").IncidentAssistant.max_attachments == 3


async def run_5_5(db, monkeypatch, capsys, user_id, team):
    app = load(f"book_5_5_{user_id}", "5-5/app.py")

    class Fake(app.IncidentLookup):
        model: ModelSettings = FakeSettings()

    monkeypatch.setattr(app, "IncidentLookup", Fake)
    model = Scripted(
        script=[
            tool_reply("open_incident", {"incident_id": "INC-7"}, "t1"),
            structured_reply("Answer", {"answer": "ok"}),
        ]
    )
    runner_with(db, model)

    await app.main(user_id, team)

    (tool_text,) = [m.content for m in model.seen[-1] if isinstance(m, ToolMessage) and m.tool_call_id == "t1"]
    assert capsys.readouterr().out.strip() == "ok"
    return tool_text


async def test_chapter_5_5_opens_an_incident_for_the_team_it_belongs_to(db, monkeypatch, capsys):
    assert await run_5_5(db, monkeypatch, capsys, "ana", "payments") == "Checkout is down for card payments."


async def test_chapter_5_5_refuses_another_team_in_text_and_the_run_goes_on(db, monkeypatch, capsys):
    assert await run_5_5(db, monkeypatch, capsys, "raj", "search") == "Not allowed."


async def test_chapter_5_5_requires_an_identity_on_every_run(db):
    app = load("book_5_5_required", "5-5/app.py")
    runner = runner_with(db, Scripted(script=[]))
    del runner
    from featherweight import AgentRunner

    strict = AgentRunner(db, identity_type=app.Responder)
    try:
        await strict.run(app.IncidentLookup, app.Question(text="?"))
    except TypeError as exc:
        assert "every run needs identity=" in str(exc)
    else:
        raise AssertionError("a run without an identity must raise when a type is declared")


def test_chapter_5_6_acts_once_for_the_right_approver_and_refuses_the_rest(capsys):
    app = load("book_5_6", "5-6/app.py")
    app.main()

    out = capsys.readouterr().out.splitlines()
    assert out == [
        'proposed: {"service":"payment-form","to_version":"4.1","reason":"errors began with 4.2"}',
        "raj -> refused: raj may not approve",
        "ana -> refused: it is not the proposal that was shown",
        "ana -> rolled payment-form back to 4.1",
        "ana -> rolled payment-form back to 4.1",
    ]
    assert list(app.DONE.values()) == ["rolled payment-form back to 4.1"]  # the second approval did not act again


def test_chapter_5_6_refuses_an_expired_and_an_already_approved_proposal():
    import pytest

    app = load("book_5_6_refusals", "5-6/app.py")
    record = app.keep(app.Rollback(service="s", to_version="1", reason="r"), seconds=-1)  # it has expired already
    with pytest.raises(PermissionError, match="expired"):
        app.approve(record["id"], record["hash"], "ana", "k1")
    fresh = app.keep(app.Rollback(service="s", to_version="1", reason="r"))
    app.approve(fresh["id"], fresh["hash"], "ana", "k2")
    with pytest.raises(PermissionError, match="not pending"):
        app.approve(fresh["id"], fresh["hash"], "ana", "k3")  # another key: it is not approved twice
    assert len(app.DONE) == 1
