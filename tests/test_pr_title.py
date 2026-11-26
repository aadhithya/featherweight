"""The title of a pull request is a Conventional Commit (`scripts/check_pr_title.py` and its workflow)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "check_pr_title.py"


def check(title: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PR_TITLE": title}
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize(
    "title",
    [
        "feat(runner): launch agents",
        "fix: handle empty input",
        "feat(config)!: every variable starts with FTW_",
        "refactor!: remove the run store",
        "chore(release): bump version to 0.2.0",  # the release pull request
    ],
)
def test_a_conventional_title_passes(title):
    assert check(title).returncode == 0


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Add the coverage gate",
        "feature: launch agents",
        "Fix: handle empty input",
        "fix:handle empty input",
        "fix: ",
        "fix(): handle empty input",
        "fix handle empty input",
        " fix: handle empty input",
        "fix: handle empty input\n::error::injected",
    ],
)
def test_another_title_fails_and_says_how_to_write_it(title):
    run = check(title)
    assert run.returncode == 1 and "type(scope): subject" in run.stdout
    assert not any(line.startswith("::") for line in run.stdout.splitlines())  # no command for the runner's log


def test_the_workflow_runs_on_an_edited_title_and_never_puts_it_in_a_command():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "pr-title.yml").read_text())
    assert set(workflow[True]["pull_request"]["types"]) == {"opened", "edited", "reopened", "synchronize"}  # `on`
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["pr-title"]["steps"]  # the name of the check, for the branch protection
    assert all("github.event" not in step.get("run", "") for step in steps)
    [step] = [step for step in steps if "run" in step]
    assert step["env"] == {"PR_TITLE": "${{ github.event.pull_request.title }}"}
