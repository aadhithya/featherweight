"""The real-model tests run here too, on fake models (`E2E_FAKE`, tests/e2e/e2e_fake.py), so that a change to the framework
cannot leave them behind. No key is used, and the fake closes the network. It says nothing about a real provider: `make e2e`
does."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TESTS_ON_EACH_PROVIDER = 8
PROVIDERS = 3


def run_e2e(tmp_path, fake: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "E2E_FAKE": fake, "FTW_DB_PATH": str(tmp_path / "featherweight.db")}
    command = [sys.executable, "-m", "pytest", "-m", "e2e", "tests/e2e", "-q", "-rs", "-p", "no:cacheprovider"]
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)


def summary(run: subprocess.CompletedProcess) -> str:
    return run.stdout.strip().splitlines()[-1]


def test_every_real_model_test_runs_on_every_provider(tmp_path):
    run = run_e2e(tmp_path, "1")
    assert run.returncode == 0, run.stdout[-6000:]
    assert re.match(rf"{TESTS_ON_EACH_PROVIDER * PROVIDERS} passed in ", summary(run)), summary(run)  # none skipped
    assert (tmp_path / "e2e.db").exists()


def test_every_real_model_test_fails_on_a_wrong_answer(tmp_path):
    run = run_e2e(tmp_path, "wrong")
    assert run.returncode == 1
    assert re.match(rf"{TESTS_ON_EACH_PROVIDER * PROVIDERS} failed in ", summary(run)), summary(run)


@pytest.mark.parametrize("provider", ["gemini", "claude", "openai"])
def test_a_provider_is_on_the_smallest_model_with_a_cap(provider, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests" / "e2e"))
    from e2e_providers import OUTPUT_CAP, PROVIDERS

    [chosen] = [p for p in PROVIDERS if p.name == provider]
    smallest = {"gemini": "gemini-3.5-flash-lite", "claude": "claude-haiku-5-5", "openai": "gpt-6-luna"}
    for reasoning in ("off", "low"):
        settings = chosen.settings(reasoning)
        assert (settings.model, settings.max_output_tokens) == (smallest[provider], OUTPUT_CAP)
