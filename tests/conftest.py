import pytest
import pytest_asyncio

from doclearn.store import LearnedStore
from micro.db import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    """A fresh sqlite database per test. (The e2e tests override it with the shared `e2e.db`.)"""
    async with Database(tmp_path / "test.db") as database:
        yield database


@pytest_asyncio.fixture
async def store(db):
    return await LearnedStore.open(db)


@pytest.fixture(autouse=True)
def no_real_llm_calls(request, monkeypatch):
    """Tests use mocks only. With no API key, anything that tries a real model fails at setup.

    The e2e tests (marker `e2e`, run with `make e2e`) are the exception: they use the real model.
    """
    if request.node.get_closest_marker("e2e"):
        return
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("MAX_CONCURRENT_AGENT_RUNS", raising=False)  # tests start from the default
