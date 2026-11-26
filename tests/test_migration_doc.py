"""docs/migration.md says what to write instead of each old name: the old names must be gone from the code, and the new ones must be there."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from featherweight import AgentRunner, AttachmentLimitError, BadRequest, SqliteDatabase, StreamingAgent
from featherweight.adapters import vercel

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
    section = section.split("4. **Move what has no one-word replacement")[0]
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
    # `background_agents`: the key of an agent's hash keeps the old name on purpose, so that no agent's hash changes. The two
    # limits: the old environment variable is gone, and a Python name of `featherweight.config` is spelled like it.
    kept = {"background_agents", "MAX_AGENT_DEPTH", "MAX_AGENT_LAUNCHES"}
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


AGENT_STEPS = PAGE.split("## For coding agents")[1]
# what 0.1 took and the next release does not: each must be in the search of the steps for coding agents
GONE_WITHOUT_A_NEW_NAME = (
    "MAX_AGENT_DEPTH MAX_AGENT_LAUNCHES MAX_CONCURRENT_AGENT_RUNS ENV_MAX_ DEFAULT_MAX_ _from_env NO_JOB "
    "observer_timeout receive_timeout cancel_poll heartbeat= lease= max_deliveries"
).split()


def test_the_search_for_coding_agents_looks_for_every_removed_name_and_argument():
    [search] = [line for line in AGENT_STEPS.splitlines() if line.strip().startswith('rg -n "')]
    looked_for = search.split('"')[1].split("|")
    assert not [name for name in GONE_WITHOUT_A_NEW_NAME if name not in looked_for]
    assert all(f"`{old}` | `FTW_{old}`" in AGENT_STEPS for old in ("MAX_AGENT_DEPTH", "MAX_AGENT_LAUNCHES"))


def test_an_old_name_of_a_limit_is_ignored_as_the_guide_says(monkeypatch):
    for name, value in (("MAX_AGENT_DEPTH", "1"), ("MAX_AGENT_LAUNCHES", "2"), ("MAX_CONCURRENT_AGENT_RUNS", "1")):
        monkeypatch.setenv(name, value)
    runner = AgentRunner(SqliteDatabase(":memory:"))  # never connected: only the limits are read
    assert (runner.max_depth, runner.max_launches, runner.max_concurrent) == (3, 10, 10)
    assert all(f"`{limit}`" in PAGE for limit in ("runner.max_depth", "runner.max_background_jobs"))


TEXT = {"type": "text", "text": "hi"}
INLINE = {"type": "file", "mediaType": "image/png", "url": "data:image/png;base64,iVBORw0KGgo="}
LINK = {"type": "file", "mediaType": "image/png", "url": "https://example.com/a.png"}


def vercel_body(*parts_of_each_message: list) -> bytes:
    messages = [
        {"id": str(i), "role": "assistant" if i % 2 else "user", "parts": parts}
        for i, parts in enumerate(parts_of_each_message)
    ]
    return json.dumps({"id": "chat", "messages": messages}).encode()


def test_what_the_guide_says_a_file_does_now():
    content: Any = vercel.parse_request(vercel_body([TEXT, INLINE])).messages[-1].content
    assert [part.type for part in content] == ["text", "image"]  # it goes to the model
    with pytest.raises(BadRequest, match="not a data: URL"):
        vercel.parse_request(vercel_body([TEXT, LINK]))


def test_max_attachments_0_refuses_a_file_even_in_an_earlier_message():
    class NoFiles(StreamingAgent):
        max_attachments = 0

    with_history = vercel.parse_request(vercel_body([TEXT, INLINE], [TEXT], [TEXT]))
    with pytest.raises(AttachmentLimitError):
        NoFiles.check_attachments(with_history)
    NoFiles.check_attachments(vercel.parse_request(vercel_body([TEXT])))


def test_the_guides_way_to_go_on_ignoring_files_does():
    [snippet] = [b for b in re.findall(r"```python\n(.*?)```", PAGE, re.S) if "def without_files" in b]
    namespace: dict = {}
    exec(snippet, namespace)

    chat = vercel.parse_request(namespace["without_files"](vercel_body([TEXT, INLINE], [TEXT], [LINK, TEXT])))

    assert [m.content for m in chat.messages if m.role == "user"] == ["hi", "hi"]


def test_the_example_env_file_sets_only_names_that_are_read():
    """It is what people copy: an old name in it sets nothing, without a word."""
    from featherweight import config

    set_there = re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M)
    theirs = {"GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"}  # not featherweight's
    known = {setting.name for setting in config.SETTINGS} | NOT_OURS | theirs
    assert not set(set_there) - known, f"in .env.example and read by nothing: {sorted(set(set_there) - known)}"
