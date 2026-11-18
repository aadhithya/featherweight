"""End-to-end test of the one-command CLI with the real agents against real Gemini: learn the incident report examples
(`samples/incident_reports`), let the Form Filler invent a case, generate. Run with `make e2e ARGS="-k cli"` (needs GOOGLE_API_KEY in
.env). Not part of `make test`. The terminal is scripted (the user accepts the invented case); the run folder is
written under pytest's tmp dir and the test prints its path.
"""

from pathlib import Path

import pytest
from doclearn import cli

SAMPLES = Path(__file__).parent.parent.parent / "samples" / "incident_reports"
CATEGORY = "incident_reports"


def test_cli_learns_fills_with_the_agent_and_generates(tmp_path, capsys):
    console = cli.Console(ask=lambda prompt: "g", edit=lambda path: pytest.fail("the editor must not open"))
    code = cli.main([str(SAMPLES), "--category", CATEGORY, "--fill", "auto", "--out-dir", str(tmp_path)], console)
    out = capsys.readouterr().out
    (folder,) = tmp_path.iterdir()
    print(f"\n[run folder] {folder}\n{out}")

    assert code == cli.DONE, out
    assert (folder / "generated.md").read_text().strip()
    assert (folder / "story.txt").read_text().strip()
    assert "{{" not in (folder / "generated.md").read_text()
    for name in ("tree.json", "template.json", "facts_schema.json", "conformance.json", "overview.md"):
        assert (folder / "learned" / name).exists(), name
    assert "Conformance of the source documents" in out and " INFO " not in out
