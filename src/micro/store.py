"""Run-scoped state in a mock Redis (in memory, transient). Keys are namespaced by job id."""

import json
from typing import Any

from fakeredis import FakeAsyncRedis, FakeServer


class RunStore:
    """Thin async JSON store over a Redis-compatible client.

    Defaults to an isolated in-memory `fakeredis` instance. Any `redis.asyncio.Redis`-compatible
    client can be passed instead, which is the whole swap to a real Redis.
    """

    def __init__(self, client: Any | None = None):
        self._client = client or FakeAsyncRedis(server=FakeServer(), decode_responses=True)

    @staticmethod
    def _key(job_id: str, key: str) -> str:
        return f"job:{job_id}:{key}"

    async def set(self, job_id: str, key: str, value: Any) -> None:
        await self._client.set(self._key(job_id, key), json.dumps(value))

    async def get(self, job_id: str, key: str) -> Any | None:
        raw = await self._client.get(self._key(job_id, key))
        return json.loads(raw) if raw is not None else None

    async def delete(self, job_id: str, key: str) -> None:
        await self._client.delete(self._key(job_id, key))

    async def close(self) -> None:
        await self._client.aclose()
