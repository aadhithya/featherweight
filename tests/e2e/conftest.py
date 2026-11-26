"""The real-model tests: one set, run on every provider of `e2e_providers.py`. See TESTING.md.

`E2E_FAKE=1` runs the same tests with scripted fake models (`e2e_fake.py`): no key is used and the network is closed, so CI
can check that the tests themselves still work. `E2E_FAKE=wrong` makes its last answer of each test wrong, and every test must fail.
"""

import os

import pytest
from e2e_providers import PROVIDERS, Provider

from featherweight import AgentRunner
from featherweight.testing import RecordingContentObserver

FAKE = os.environ.get("E2E_FAKE", "")


@pytest.fixture(params=PROVIDERS, ids=lambda p: p.name)
def provider(request, monkeypatch) -> Provider:
    chosen: Provider = request.param
    if FAKE:
        from e2e_fake import install

        install(request, monkeypatch, wrong=FAKE == "wrong")
    elif not chosen.has_key():
        pytest.skip(f"needs {' or '.join(chosen.keys)} in .env")
    return chosen


@pytest.fixture
def observer():
    """Keeps what the runs did, in memory: the run log holds no outputs. What was used is printed after the test."""
    kept = RecordingContentObserver()
    yield kept
    calls = kept.records("model_call_finished")
    attempts = kept.records("attempt.finish")
    print(
        f"\n[used] attempts={len(attempts)} model_calls={len(calls)} "
        f"in={sum(c.input_tokens or 0 for c in calls)} out={sum(c.output_tokens or 0 for c in calls)}"
    )


@pytest.fixture
def runner(db, observer) -> AgentRunner:
    """One attempt for each run: a real-model test does not pay for retries, and does not hide a first failure."""
    return AgentRunner(db, max_attempts=1, observers=[observer])
