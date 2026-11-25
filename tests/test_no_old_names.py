"""The names that epic #215 retired are gone from the code and the docs. The migration owns the old names, because it renames what they made."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every name that `docs/specs/units.md` retires: the Python names, the column and table names, the settings, the span attributes and the log lines.
OLD = re.compile(
    r"agent_run_id|invocation_id|agent_runs|"
    r"\b(?:RunStatus|AgentRunRow|AgentRunFailed|RunStart|RunEnd|ReusedRun|CURRENT_AGENT_RUN_ID|"
    r"mark_run_running|create_run|finish_run|get_run|list_runs|find_reusable_run|"
    r"max_background|DEFAULT_MAX_BACKGROUND|MAX_CONCURRENT_AGENT_RUNS|"
    r"invocation_started|invocation_finished|run_created|run_running)\b|"
    r"ftw\.invocation\.id"
)
# Where the old names are the subject: the migration, the migration tests (they build a database as it was), and the tests of this rule.
OWNERS = {
    "src/featherweight/migrations.py",
    "tests/test_migrations.py",
    "tests/test_postgres.py",
    "tests/test_identity_hash.py",
    "tests/test_docs_terms.py",
    "tests/test_log_lines.py",  # it checks that no line carries a retired key
    "tests/test_no_old_names.py",
}
# Pages that say what was retired, or renamed by a migration.
PAGES = {
    "docs/specs/units.md",
    "docs/specs/persistence.md",  # the migrations: it says what each one renamed
    "docs/migration.md",
    "docs/book/07-reference/03-the-glossary.md",
}


def files(folder: str, pattern: str):
    for path in (ROOT / folder).rglob(pattern):
        if "egg-info" not in path.parts and "__pycache__" not in path.parts:
            yield path


def hits(paths) -> list[str]:
    found = []
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        if rel in OWNERS or rel in PAGES:
            continue
        found += [f"{rel}: {m.group(0)}" for m in OLD.finditer(path.read_text())]
    return found


def test_the_retired_names_are_not_in_the_code():
    found = hits(p for folder in ("src", "examples", "docs/book/code", "scripts") for p in files(folder, "*.py"))
    assert not found, found


def test_the_retired_names_are_not_in_the_tests_except_where_they_are_the_subject():
    assert not hits(files("tests", "*.py")) and not hits([ROOT / "conftest.py"])


def test_the_retired_names_are_not_in_the_docs_except_where_they_are_the_subject():
    pages = [p for folder in ("docs", "examples") for p in files(folder, "*.md")] + [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
    ]
    assert not hits(pages)


def test_the_check_does_find_a_retired_name():
    for name in (
        "agent_run_id",
        "list_runs(",
        "RunStatus.SUCCESS",
        "ftw.invocation.id",
        "max_background=2",
        "run_created attempt_id=1",
    ):
        assert OLD.search(name), name
    for name in ("max_background_jobs", "attempt_created", "run_started", "list_attempts", "ftw.run.id"):
        assert not OLD.search(name), name
