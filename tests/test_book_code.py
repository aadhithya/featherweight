"""The code of the book's chapters, on a fake model: what a reader types is what runs."""

import importlib.util
from pathlib import Path

from fakes import FakeSettings, Streams, runner_with

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
