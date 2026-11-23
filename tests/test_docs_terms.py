"""The docs use the words of the glossary (docs/book/07-reference/03-the-glossary.md): a name that it rules out fails here.

Only prose is read: code blocks, code spans, link targets and comments are left alone, so a field called `subagents` is fine."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = ROOT / "docs" / "book" / "07-reference" / "03-the-glossary.md"

# (a name the glossary rules out, what to write instead)
RULED_OUT = [
    (r"\bsubagents?\b", "sub-agent"),
    (r"\bbackground agents?\b", "background job"),
    (r"\bjob workers?\b", "worker"),
    (r"\bchat agents?\b", "streaming agent"),
    (r"\brun stores?\b", "run log"),
    (r"\bresults stores?\b", "result store"),
    (r"\ballowlists?\b|\bwhitelists?\b", "allow-list"),
    (r"\bFeatherweight\b", "featherweight (lowercase)"),
    (r"\bprincipal\b", "identity"),
    (r"\bnothing has been released\b", "(the first release is out)"),
]
# where a ruled-out name is used on purpose
ALLOWED = {
    "docs/specs/tools-and-store.md": {r"\brun stores?\b"},  # it recounts that the run store was removed
    "docs/migration.md": {r"\brun stores?\b"},  # it says what to do about the removal
}

PROTECTED = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`|\]\([^)]*\)|<!--.*?-->|<https?://[^>]+>|https?://\S+)", re.S)


def prose(text: str) -> str:
    return "".join(part for i, part in enumerate(PROTECTED.split(text)) if i % 2 == 0)


def pages() -> list[Path]:
    found = [*ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/**/*.md")]
    found += [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "skills" / "featherweight" / "SKILL.md"]
    return sorted(p for p in found if p != GLOSSARY)


@pytest.mark.parametrize("pattern,use", RULED_OUT, ids=[use for _, use in RULED_OUT])
def test_the_docs_do_not_use_a_name_that_the_glossary_rules_out(pattern, use):
    found = []
    for page in pages():
        if pattern in ALLOWED.get(page.relative_to(ROOT).as_posix(), set()):
            continue
        for line in prose(page.read_text()).splitlines():
            if re.search(pattern, line, re.I if pattern != r"\bFeatherweight\b" else 0):
                found.append(f"{page.relative_to(ROOT)}: {line.strip()[:90]}")
    assert not found, f"use {use!r}:\n" + "\n".join(found[:15])


def test_the_glossary_says_which_names_are_ruled_out():
    text = GLOSSARY.read_text()
    assert "Words we use, and words we do not" in text
    for use in ("sub-agent", "background job", "worker", "run log", "streaming agent", "identity"):
        assert use in text.split("Words we use, and words we do not")[1]


def test_the_source_docstrings_use_the_same_words():
    found = []
    for path in sorted((ROOT / "src" / "featherweight").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"background agents?\b|job workers?\b", line, re.I):
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:90]}")
    assert not found, "\n".join(found)


def test_the_check_reads_prose_only_and_does_catch_a_ruled_out_name():
    assert re.search(RULED_OUT[0][0], prose("An agent can launch a subagent."), re.I)
    assert not re.search(
        RULED_OUT[0][0], prose("The `subagents` field, and a [link](launching-agents.md#subagents)."), re.I
    )
    assert not re.search(RULED_OUT[0][0], prose("```python\nsubagent = 1\n```"), re.I)
