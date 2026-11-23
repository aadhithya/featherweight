"""The names that epic #215 retired are gone from the code. The migration owns the old names, because it renames what they made."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD = re.compile(
    r"agent_run_id|invocation_id|\bRunStatus\b|\bAgentRunRow\b|\bRunStart\b|\bRunEnd\b|CURRENT_AGENT_RUN_ID|agent_runs"
)
OWNERS = {"src/featherweight/migrations.py"}


def test_the_retired_names_are_not_in_the_code():
    found = []
    for folder in ("src", "examples"):
        for path in (ROOT / folder).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in OWNERS:
                continue
            found += [f"{rel}: {m.group(0)}" for m in OLD.finditer(path.read_text())]
    assert not found, found
