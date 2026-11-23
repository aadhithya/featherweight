"""docs/migration.md says what to write instead of each old name: the old names must be gone from the code, and the new ones must be there."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "docs" / "migration.md").read_text()
SRC = "\n".join(p.read_text() for p in (ROOT / "src").rglob("*.py") if p.name != "migrations.py")
TOKEN = re.compile(r"`([^`]+)`")


def python_table() -> list[tuple[list[str], list[str]]]:
    section = PAGE.split("**Python**")[1].split("**SQL**")[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 2 and not set(cells[0]) <= {"-", " "} and cells[0] != "Before":
            rows.append((TOKEN.findall(cells[0]), TOKEN.findall(cells[1])))
    return rows


# words of the table that are not renamed: the objects the names hang on, and the arguments
KEPT = {
    "db",
    "featherweight",
    "AgentRunner",
    "result",
    "row",
    "job_row",
    "runtime",
    "context",
    "job_id",
    "agent_hash",
    "input_hash",
    "identity_hash",
}


def identifiers(tokens: list[str]) -> set[str]:
    return {i for token in tokens for i in re.findall(r"[A-Za-z_]\w*", token)} - KEPT


def test_the_table_has_rows():
    assert len(python_table()) >= 12


def test_every_old_name_of_the_table_is_gone_from_the_code():
    stale = [
        n
        for before, after in python_table()
        for n in identifiers(before) - identifiers(after)
        if re.search(rf"\b{n}\b", SRC)
    ]
    assert not stale, f"still in src/: {sorted(set(stale))}"


def test_every_new_name_of_the_table_is_in_the_code():
    missing = [
        n
        for before, after in python_table()
        for n in identifiers(after) - identifiers(before)
        if not re.search(rf"\b{n}\b", SRC)
    ]
    assert not missing, f"not in src/: {sorted(set(missing))}"
