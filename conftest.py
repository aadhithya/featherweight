"""Shared test setup for `tests/` (the harness) and `examples/*/tests` (the examples).

Unit tests use mocks only. Tests in a folder called `e2e` use the real model: they are marked `e2e`, left out of
`make test`, and run with `make e2e`.
"""

import os
from pathlib import Path

import pytest
import pytest_asyncio
from fakes import MODELS

from featherweight import config
from featherweight.log import setup_logging
from featherweight.sqlite import SqliteDatabase


def is_e2e(node) -> bool:
    return node.get_closest_marker("e2e") is not None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Every test in a folder called `e2e` is an e2e test (real model calls): mark it, so `make test` leaves it out."""
    for item in items:
        if "e2e" in item.path.relative_to(config.rootpath).parts:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(autouse=True)
def no_real_llm_calls(request, monkeypatch):
    """Tests use mocks only. With no API key, anything that tries a real model fails at setup.

    The e2e tests are the exception: they use the real model.
    """
    if is_e2e(request.node):
        return
    MODELS.clear()  # a test registers its own fake models (tests/fakes.py)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for setting in config.SETTINGS:
        monkeypatch.delenv(setting.name, raising=False)  # tests start from the defaults...
    monkeypatch.setenv(config.WORKER_RECEIVE_TIMEOUT.name, "0.05")  # ...except that a worker in a test looks at once
    monkeypatch.setenv(config.WORKER_CANCEL_POLL.name, "0.05")


@pytest.fixture(autouse=True)
def e2e_setup(request):
    """The e2e tests need the API key (they are skipped without it) and log to stdout."""
    if not is_e2e(request.node):
        return
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        pytest.skip("needs GOOGLE_API_KEY in .env")
    setup_logging()


@pytest_asyncio.fixture
async def db(request, tmp_path):
    """A fresh sqlite database per test.

    The e2e tests share one database, `e2e.db`, next to `FTW_DB_PATH` (so, in the project directory, which is
    mounted into the container, it is on the host). Every agent input and output is in its `attempts` table, so a
    failed run can be inspected afterwards without new model calls, e.g.
    `sqlite3 e2e.db "select agent_name, status, error from attempts"`. Rows accumulate across runs; tell them apart
    by job_id and created_at. `make clean-all` deletes it.
    """
    if is_e2e(request.node):
        path = Path(os.environ.get("FTW_DB_PATH", "featherweight.db")).parent / "e2e.db"
        print(f"\n[db] {path}")
    else:
        path = tmp_path / "test.db"
    async with SqliteDatabase(path) as database:
        yield database
