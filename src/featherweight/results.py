"""The results that resuming a job reads back, behind an abstract class the user can implement.

Featherweight keeps the state of the orchestration in the run log, and never the data that flowed through it. The one place
where an output is kept is a `ResultStore`, because resuming a job needs the output of a step that already succeeded, and only
for a job that opted in (`resumable=True`). The default is a sqlite store in the file of the run log, which is for local
development; a deployment passes its own store, or none. See docs/specs/persistence.md.
"""

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from featherweight.db import Database

HASH = r"^[0-9a-f]{64}$"  # a SHA-256, in hex


class ResultKey(BaseModel):
    """What identifies one stored output: the job it was made in, the agent (the hash of its whole declaration) and the input
    (the hash of it). The same agent with the same input in another job is another key."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    agent_hash: str = Field(pattern=HASH)
    input_hash: str = Field(pattern=HASH)


class ResultStore(ABC):
    """Where the outputs for resuming are kept. Subclass it and write the three methods; one that misses a method cannot be built.

    An output is JSON: a `dict`. The contract (`featherweight.testing.check_result_store` runs it against any store):
    - `get` returns what `put` stored for the key, equal as JSON, or `None` when there is none;
    - `put` on a key that exists replaces it;
    - calls can be concurrent;
    - `delete_job` removes every result of a job, and does not fail when there are none.
    """

    @abstractmethod
    async def put(self, key: ResultKey, output: dict[str, Any]) -> None:
        """Keep `output` under `key`, replacing what was there."""

    @abstractmethod
    async def get(self, key: ResultKey) -> dict[str, Any] | None:
        """What was kept under `key`, or None."""

    @abstractmethod
    async def delete_job(self, job_id: str) -> None:
        """Forget every result of the job."""


class SqliteResultStore(ResultStore):
    """The default store: a `results` table in the file of the run log, through its connection. For local development."""

    def __init__(self, db: Database):
        self.db = db

    async def put(self, key: ResultKey, output: dict[str, Any]) -> None:
        await self.db.conn.execute(
            """INSERT INTO results (job_id, agent_hash, input_hash, output, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (job_id, agent_hash, input_hash) DO UPDATE SET output = excluded.output,
               created_at = excluded.created_at""",
            (key.job_id, key.agent_hash, key.input_hash, json.dumps(output), datetime.now(UTC).isoformat()),
        )
        await self.db.conn.commit()

    async def get(self, key: ResultKey) -> dict[str, Any] | None:
        async with self.db.conn.execute(
            "SELECT output FROM results WHERE job_id = ? AND agent_hash = ? AND input_hash = ?",
            (key.job_id, key.agent_hash, key.input_hash),
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row["output"]) if row else None

    async def delete_job(self, job_id: str) -> None:
        await self.db.conn.execute("DELETE FROM results WHERE job_id = ?", (job_id,))
        await self.db.conn.commit()
