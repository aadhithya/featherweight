import os
from pathlib import Path

import pytest
import pytest_asyncio

from micro.db import Database
from micro.log import setup_logging

HERE = Path(__file__).parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Every test in this folder is an e2e test (real model calls): mark it, so `make test` leaves it out."""
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(autouse=True)
def need_api_key():
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        pytest.skip("needs GOOGLE_API_KEY in .env")


@pytest.fixture(autouse=True)
def logging_to_stdout():
    setup_logging()


@pytest_asyncio.fixture
async def db():
    """All e2e tests share one database, `e2e.db`, in the project directory (bind-mounted, so it is on the host).

    Every agent input and output is in its `agent_runs` table, so a failed run can be inspected afterwards
    without new model calls, e.g. `sqlite3 e2e.db "select agent_name, status, error from agent_runs"`.
    Rows accumulate across runs; tell them apart by job_id and created_at. `make clean-all` deletes it.
    """
    path = Path(os.environ.get("MICRO_DB_PATH", "micro.db")).parent / "e2e.db"
    print(f"\n[db] {path}")
    async with Database(path) as database:
        yield database
