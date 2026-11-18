"""sqlite run log: `jobs`, and `agent_runs` with one row per attempt."""

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel

from micro.models import RunStatus

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    agent_run_id        TEXT PRIMARY KEY,
    invocation_id       TEXT NOT NULL,
    attempt             INTEGER NOT NULL,
    job_id              TEXT REFERENCES jobs(job_id),
    agent_name          TEXT NOT NULL,
    agent_version       TEXT NOT NULL,
    model_name          TEXT NOT NULL,
    reported_model_name TEXT,
    reasoning           TEXT NOT NULL,
    tool_names          TEXT NOT NULL,
    system_prompt_hash  TEXT NOT NULL,
    status              TEXT NOT NULL,
    input               TEXT NOT NULL,
    output              TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    error               TEXT,
    latency_ms          REAL,
    created_at          TEXT NOT NULL,
    completed_at        TEXT,
    parent_agent_run_id TEXT,
    depth               INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_job ON agent_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_invocation ON agent_runs(invocation_id);
"""

# Columns added after the first version of the table: an older database gets them when it is opened.
ADDED_COLUMNS = {
    "parent_agent_run_id": "TEXT",  # the attempt of the agent that launched this one; NULL for a top-level run
    "depth": "INTEGER NOT NULL DEFAULT 1",  # 1 for a top-level run, 2 for the agent it launched, and so on
}


def new_id() -> str:
    return uuid.uuid4().hex


def default_db_path() -> Path:
    return Path(os.environ.get("MICRO_DB_PATH", "micro.db"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(value: BaseModel | dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value)


class JobRow(BaseModel):
    job_id: str
    name: str
    status: RunStatus
    created_at: str
    completed_at: str | None
    error: str | None


class AgentRunRow(BaseModel):
    agent_run_id: str
    invocation_id: str
    attempt: int
    job_id: str | None
    agent_name: str
    agent_version: str
    model_name: str
    reported_model_name: str | None
    reasoning: str
    tool_names: list[str]
    system_prompt_hash: str
    status: RunStatus
    input: dict[str, Any]
    output: dict[str, Any] | None
    input_tokens: int | None
    output_tokens: int | None
    error: str | None
    latency_ms: float | None
    created_at: str
    completed_at: str | None
    parent_agent_run_id: str | None = None
    depth: int = 1


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        logger.info("db_connected path=%s", self.path)

    async def _migrate(self) -> None:
        async with self._conn.execute("PRAGMA table_info(agent_runs)") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        for column, definition in ADDED_COLUMNS.items():
            if column not in existing:
                await self._conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {column} {definition}")
                logger.info("db_migrated column=%s", column)
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_agent_run_id)")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("db_closed path=%s", self.path)

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    # jobs

    async def create_job(self, name: str, job_id: str | None = None) -> str:
        job_id = job_id or new_id()
        await self.conn.execute(
            "INSERT INTO jobs (job_id, name, status, created_at) VALUES (?, ?, ?, ?)",
            (job_id, name, RunStatus.INIT.value, _now()),
        )
        await self.conn.commit()
        logger.info("job_created job_id=%s name=%s", job_id, name)
        return job_id

    async def set_job_status(self, job_id: str, status: RunStatus, error: str | None = None) -> None:
        """Set the job's status. `error` is kept only for FAILED; any other status clears it."""
        status = RunStatus(status)
        terminal = status in (RunStatus.SUCCESS, RunStatus.FAILED)
        error = error if status is RunStatus.FAILED else None
        cur = await self.conn.execute(
            "UPDATE jobs SET status = ?, completed_at = ?, error = ? WHERE job_id = ?",
            (status.value, _now() if terminal else None, error, job_id),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"No such job: {job_id}")
        log = logger.warning if status is RunStatus.FAILED else logger.info
        log("job_status job_id=%s status=%s error=%s", job_id, status.value, error)

    async def get_job(self, job_id: str) -> JobRow | None:
        async with self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return JobRow(**dict(row)) if row else None

    # agent_runs

    async def create_run(
        self,
        *,
        agent_run_id: str,
        invocation_id: str,
        attempt: int,
        job_id: str | None,
        agent_name: str,
        agent_version: str,
        model_name: str,
        reasoning: str,
        tool_names: list[str],
        system_prompt_hash: str,
        input: BaseModel | dict[str, Any],
        parent_agent_run_id: str | None = None,
        depth: int = 1,
    ) -> str:
        await self.conn.execute(
            """INSERT INTO agent_runs (
                   agent_run_id, invocation_id, attempt, job_id, agent_name, agent_version,
                   model_name, reasoning, tool_names, system_prompt_hash, status, input, created_at,
                   parent_agent_run_id, depth
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_run_id,
                invocation_id,
                attempt,
                job_id,
                agent_name,
                agent_version,
                model_name,
                reasoning,
                json.dumps(tool_names),
                system_prompt_hash,
                RunStatus.INIT.value,
                _dump(input),
                _now(),
                parent_agent_run_id,
                depth,
            ),
        )
        await self.conn.commit()
        logger.info(
            "run_created agent_run_id=%s invocation_id=%s attempt=%d job_id=%s agent=%s version=%s model=%s "
            "parent_agent_run_id=%s depth=%d",
            agent_run_id,
            invocation_id,
            attempt,
            job_id,
            agent_name,
            agent_version,
            model_name,
            parent_agent_run_id,
            depth,
        )
        return agent_run_id

    async def mark_run_running(self, agent_run_id: str) -> None:
        await self._set_run(agent_run_id, status=RunStatus.RUNNING.value)
        logger.info("run_running agent_run_id=%s", agent_run_id)

    async def finish_run(
        self,
        agent_run_id: str,
        *,
        status: RunStatus,
        output: BaseModel | dict[str, Any] | None = None,
        error: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: float | None = None,
        reported_model_name: str | None = None,
    ) -> None:
        status = RunStatus(status)
        if status not in (RunStatus.SUCCESS, RunStatus.FAILED):
            raise ValueError("finish_run needs a terminal status (SUCCESS or FAILED)")
        await self._set_run(
            agent_run_id,
            status=status.value,
            output=_dump(output),
            completed_at=_now(),
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            reported_model_name=reported_model_name,
        )
        log = logger.info if status is RunStatus.SUCCESS else logger.warning
        log(
            "run_finished agent_run_id=%s status=%s input_tokens=%s output_tokens=%s latency_ms=%s error=%s",
            agent_run_id,
            status.value,
            input_tokens,
            output_tokens,
            latency_ms,
            error,
        )

    async def get_run(self, agent_run_id: str) -> AgentRunRow | None:
        async with self.conn.execute("SELECT * FROM agent_runs WHERE agent_run_id = ?", (agent_run_id,)) as cur:
            row = await cur.fetchone()
        return self._row_to_run(row) if row else None

    async def list_runs(self, *, job_id: str | None = None, invocation_id: str | None = None) -> list[AgentRunRow]:
        """Runs in insertion order, optionally filtered by job and/or invocation."""
        where, params = [], []
        if job_id is not None:
            where.append("job_id = ?")
            params.append(job_id)
        if invocation_id is not None:
            where.append("invocation_id = ?")
            params.append(invocation_id)
        sql = "SELECT * FROM agent_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid"
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_run(r) for r in rows]

    async def _set_run(self, agent_run_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{c} = ?" for c in columns)
        cur = await self.conn.execute(
            f"UPDATE agent_runs SET {assignments} WHERE agent_run_id = ?",
            (*columns.values(), agent_run_id),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"No such run: {agent_run_id}")

    @staticmethod
    def _row_to_run(row: aiosqlite.Row) -> AgentRunRow:
        data = dict(row)
        data["tool_names"] = json.loads(data["tool_names"])
        data["input"] = json.loads(data["input"])
        data["output"] = json.loads(data["output"]) if data["output"] is not None else None
        return AgentRunRow(**data)
