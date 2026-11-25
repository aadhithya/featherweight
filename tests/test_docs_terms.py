"""The docs use the words of the glossary (docs/book/07-reference/03-the-glossary.md): a name that it rules out fails here.

Only prose is read: code blocks, code spans, link targets and comments are left alone, so a field called `subagents` is fine."""

import ast
import io
import re
import tokenize
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
    (r"\binvocations?\b", "run"),
    (r"\b(?:an?|each|every|one) agent runs?\b", "run"),
]
# where a ruled-out name is used on purpose
ALLOWED = {
    "docs/specs/units.md": {
        r"\binvocations?\b",
        r"\b(?:an?|each|every|one) agent runs?\b",
    },  # it retires them and says what they were
    "docs/book/07-reference/03-the-glossary.md": {
        r"\binvocations?\b",
        r"\b(?:an?|each|every|one) agent runs?\b",
    },  # it lists them as ruled out
    "docs/specs/tools.md": {r"\brun stores?\b"},  # it recounts that the run store was removed
    "docs/migration.md": {
        r"\brun stores?\b",
        r"\binvocations?\b",
        r"\b(?:an?|each|every|one) agent runs?\b",
    },  # it says what to do about the removal and the renames
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


CODE_RULED_OUT = [(pattern, use) for pattern, use in RULED_OUT if use == "run"] + [(r"\bagent runs are\b", "run")]


def words_of_code(path: Path) -> list[tuple[int, str]]:
    """The comments, and the docstrings, of a Python file: the words a reader meets in it, with their line."""
    source = path.read_text()
    found = [
        (t.start[0], t.string)
        for t in tokenize.generate_tokens(io.StringIO(source).readline)
        if t.type == tokenize.COMMENT
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc and node.body:
                found.append((node.body[0].lineno, doc))
    return found


def test_the_comments_and_docstrings_of_the_books_code_and_the_examples_use_the_same_words():
    found = []
    for folder in ("docs/book/code", "examples"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            for number, text in words_of_code(path):
                for pattern, use in CODE_RULED_OUT:
                    if re.search(pattern, text, re.I):
                        found.append(f"{path.relative_to(ROOT)}:{number}: say {use!r}: {text.strip()[:80]}")
    assert not found, "\n".join(found)


def test_the_check_reads_prose_only_and_does_catch_a_ruled_out_name():
    assert re.search(RULED_OUT[0][0], prose("An agent can launch a subagent."), re.I)
    assert not re.search(
        RULED_OUT[0][0], prose("The `subagents` field, and a [link](launching-agents.md#subagents)."), re.I
    )
    assert not re.search(RULED_OUT[0][0], prose("```python\nsubagent = 1\n```"), re.I)


def test_the_check_of_code_reads_comments_and_docstrings_and_does_catch_a_ruled_out_name(tmp_path):
    path = tmp_path / "chapter.py"
    path.write_text('"""Its agent runs are in the run log."""\n\nx = "an agent run"  # one agent run, inside the job\n')
    words = [text for _, text in words_of_code(path)]
    assert any("agent runs are" in text for text in words) and any("one agent run" in text for text in words)
    assert not any('"an agent run"' in text for text in words)  # a string that is not a docstring is not read
    assert all(any(re.search(p, t, re.I) for p, _ in CODE_RULED_OUT) for t in words)
