import pytest_asyncio

from doclearn.store import LearnedStore


@pytest_asyncio.fixture
async def store(db):
    return await LearnedStore.open(db)
