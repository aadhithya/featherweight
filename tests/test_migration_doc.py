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
        if (
            line.strip().startswith("|")
            and len(cells) == 2
            and not set(cells[0]) <= {"-", " "}
            and cells[0] != "Before"
        ):
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


# the other tables of the page: the settings, the log lines, the span attributes, the SQL and the steps for coding agents

SECTION = PAGE.split("## Settings, logs and spans")[1].split("## Your database upgrades by itself")[0]
OTEL = (ROOT / "src" / "featherweight" / "otel.py").read_text()
MIGRATION_SQL = (ROOT / "src" / "featherweight" / "migrations.py").read_text()
NOT_OURS = {"FTW_DB_PATH", "FTW_LOG_LEVEL"}  # read where they always were, and not in `SETTINGS`


def rows(section: str) -> list[list[str]]:
    found = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if (
            line.strip().startswith("|")
            and len(cells) >= 2
            and not set(cells[0]) <= {"-", " "}
            and cells[0] != "Before"
        ):
            found.append(cells)
    return found


def steps_table() -> list[list[str]]:
    section = (
        PAGE.split("**Rename mechanically**")[1]
        if "**Rename mechanically**" in PAGE
        else PAGE.split("3. **Rename mechanically,**")[1]
    )
    section = section.split("4. **Ask the owner**")[0]
    return [r for r in rows(section) if r[0] not in ("#",) and len(r) >= 3]


def test_the_settings_table_has_rows():
    assert len(rows(SECTION)) >= 8


def test_every_variable_of_the_page_is_one_that_featherweight_reads():
    from featherweight import config

    known = {s.name for s in config.SETTINGS} | NOT_OURS
    named = set(re.findall(r"\bFTW_[A-Z_]+\b", PAGE))
    assert named and not named - known, f"not read by featherweight: {sorted(named - known)}"


def test_every_new_span_attribute_of_the_page_is_written_by_the_code_and_every_old_one_is_gone():
    pairs = [
        (set(re.findall(r"`(ftw\.[a-z_.]+)`", b)), set(re.findall(r"`(ftw\.[a-z_.]+)`", a)))
        for b, a, *_ in rows(SECTION)
    ]
    new = {n for _, after in pairs for n in after}
    old = {
        n for before, _ in pairs for n in before
    } - new  # `ftw.run.id` is an old name and a new one: it changed its meaning
    assert new >= {"ftw.run.id", "ftw.attempt.id", "ftw.reused_from_attempt_id"}
    assert not [n for n in new if f'"{n}"' not in OTEL], "not attributes of otel.py"
    assert not [n for n in old if f'"{n}"' in OTEL], "still attributes of otel.py"


def test_every_new_log_line_and_key_of_the_page_is_written_by_the_code_and_every_old_one_is_gone():
    pairs = [
        ({t for t in re.findall(r"`([a-z_]+=?)`", b)}, {t for t in re.findall(r"`([a-z_]+=?)`", a)})
        for b, a, *_ in rows(SECTION)
        if re.search(r"log lines?|keys", b)
    ]
    new = {n for _, after in pairs for n in after}
    old = {
        n for before, _ in pairs for n in before
    } - new  # `run_finished` was a line of the rows, and is a line of the runs
    assert new >= {"run_started", "attempt_created", "attempt_id=", "run_id="}
    assert not [n for n in new if n not in SRC], "not in src/"
    assert not [n for n in old if re.search(rf"(?<![A-Za-z_]){re.escape(n)}", SRC)], "still in src/"


def test_every_name_of_the_sql_line_is_in_the_schema():
    sql = PAGE.split("**SQL**")[1].split("\n\n")[0]
    tokens = set(re.findall(r"`([a-z_]+)`", sql))
    assert {"attempts", "attempt_id", "run_id", "parent_attempt_id", "reused_from_attempt_id"} <= tokens
    missing = [t for t in tokens if t not in MIGRATION_SQL]
    assert not missing, f"not in migrations.py: {missing}"


def test_the_steps_for_coding_agents_find_names_that_are_gone_and_replace_them_with_names_that_are_there():
    table = steps_table()
    assert len(table) >= 15
    replacements = {t for row in table for t in TOKEN.findall(row[2])}
    kept = {
        "background_agents"
    }  # the key of an agent's hash keeps the old name on purpose, so that no agent's hash changes
    gone = [
        t
        for row in table
        for t in TOKEN.findall(row[1])
        if t not in replacements
        and t not in kept
        and re.search(rf"(?<![A-Za-z_]){re.escape(t)}(?![A-Za-z_])", SRC + OTEL)
    ]
    assert not gone, f"still in src/: {gone}"
    absent = [t for t in replacements if t not in SRC + OTEL and not t.startswith("`")]
    assert not absent, f"not in src/: {absent}"
