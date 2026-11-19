"""`SqliteDatabase`: the run log in a sqlite file. For local development; deployments use `PostgresDatabase`."""

import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from featherweight.db import AgentRunRow, Database, JobRow, RunEnd, RunStart, now
from featherweight.errors import SetupError
from featherweight.migrations import MIGRATIONS
from featherweight.models import RunStatus

logger = logging.getLogger(__name__)

DIALECT = "sqlite"


def _stamp(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


class SqliteDatabase(Database):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        try:
            await self._migrate()
        except BaseException:
            await self.close()
            raise
        logger.info("db_connected path=%s", self.path)

    async def _tables(self) -> set[str]:
        async with self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
            return {row["name"] for row in await cur.fetchall()}

    async def _migrate(self) -> None:
        tables = await self._tables()
        if "schema_migrations" not in tables and "agent_runs" in tables:
            raise SetupError(
                f"{self.path} is from before migrations (an older version of the run log). Delete it and run again: "
                "nothing is carried over."
            )
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        async with self.conn.execute("SELECT version FROM schema_migrations") as cur:
            applied = {row["version"] for row in await cur.fetchall()}
        for migration in sorted(MIGRATIONS, key=lambda m: m.version):
            if migration.version in applied:
                continue
            script = (
                f"BEGIN;\n{migration.sql[DIALECT]}\n"
                f"INSERT INTO schema_migrations VALUES ({migration.version}, '{migration.name}', '{now().isoformat()}');\n"
                "COMMIT;"
            )
            try:
                await self.conn.executescript(script)
            except BaseException:
                with contextlib.suppress(Exception):
                    await self.conn.execute("ROLLBACK")
                raise
            logger.info("db_migrated version=%d name=%s", migration.version, migration.name)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("db_closed path=%s", self.path)

    # jobs

    async def insert_job(self, job_id: str, name: str, resumed_from: str | None, resumable: bool) -> str:
        await self.conn.execute(
            "INSERT INTO jobs (job_id, name, status, resumable, resumed_from, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, name, RunStatus.INIT.value, int(resumable), resumed_from, now().isoformat()),
        )
        await self.conn.commit()
        logger.info("job_created job_id=%s name=%s resumed_from=%s resumable=%s", job_id, name, resumed_from, resumable)
        return job_id

    async def set_job_status(
        self, job_id: str, status: RunStatus, *, error_type: str | None = None, error_hash: str | None = None
    ) -> None:
        status = RunStatus(status)
        terminal = status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED)
        failed = status is RunStatus.FAILED
        cur = await self.conn.execute(
            "UPDATE jobs SET status = ?, completed_at = ?, error_type = ?, error_hash = ? WHERE job_id = ?",
            (
                status.value,
                now().isoformat() if terminal else None,
                error_type if failed else None,
                error_hash if failed else None,
                job_id,
            ),
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"No such job: {job_id}")
        log = logger.warning if failed else logger.info
        log("job_status job_id=%s status=%s error_type=%s", job_id, status.value, error_type if failed else None)

    async def get_job(self, job_id: str) -> JobRow | None:
        async with self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return self._job(row) if row else None

    @staticmethod
    def _job(row: aiosqlite.Row) -> JobRow:
        data: dict[str, Any] = dict(row)
        data["resumable"] = bool(data["resumable"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["completed_at"] = datetime.fromisoformat(data["completed_at"]) if data["completed_at"] else None
        return JobRow(**data)

    # agent runs

    async def create_run(self, run: RunStart) -> str:
        await self.conn.execute(
            """INSERT INTO agent_runs (
                   agent_run_id, invocation_id, attempt, job_id, parent_agent_run_id, depth, agent_name, agent_version,
                   agent_hash, system_prompt_hash, tools_hash, provider, model_name, reasoning, settings, input_hash,
                   reused_from_agent_run_id, thread_id, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.agent_run_id,
                run.invocation_id,
                run.attempt,
                run.job_id,
                run.parent_agent_run_id,
                run.depth,
                run.agent_name,
                run.agent_version,
                run.agent_hash,
                run.system_prompt_hash,
                run.tools_hash,
                run.provider,
                run.model_name,
                run.reasoning,
                json.dumps(run.settings),
                run.input_hash,
                run.reused_from_agent_run_id,
                run.thread_id,
                RunStatus.INIT.value,
                now().isoformat(),
            ),
        )
        await self.conn.commit()
        logger.info(
            "run_created agent_run_id=%s invocation_id=%s attempt=%d job_id=%s agent=%s version=%s model=%s "
            "parent_agent_run_id=%s depth=%d",
            run.agent_run_id,
            run.invocation_id,
            run.attempt,
            run.job_id,
            run.agent_name,
            run.agent_version,
            run.model_name,
            run.parent_agent_run_id,
            run.depth,
        )
        return run.agent_run_id

    async def mark_run_running(self, agent_run_id: str) -> None:
        await self._set_run(agent_run_id, status=RunStatus.RUNNING.value, started_at=now().isoformat())
        logger.info("run_running agent_run_id=%s", agent_run_id)

    async def finish_run(self, agent_run_id: str, end: RunEnd) -> None:
        await self._set_run(
            agent_run_id,
            status=end.status.value,
            completed_at=now().isoformat(),
            output_hash=end.output_hash,
            error_type=end.error_type,
            error_hash=end.error_hash,
            input_tokens=end.input_tokens,
            output_tokens=end.output_tokens,
            latency_ms=end.latency_ms,
            reported_model_name=end.reported_model_name,
        )
        log = logger.warning if end.status is RunStatus.FAILED else logger.info
        log(
            "run_finished agent_run_id=%s status=%s input_tokens=%s output_tokens=%s latency_ms=%s error_type=%s",
            agent_run_id,
            end.status.value,
            end.input_tokens,
            end.output_tokens,
            end.latency_ms,
            end.error_type,
        )

    async def get_run(self, agent_run_id: str) -> AgentRunRow | None:
        async with self.conn.execute("SELECT * FROM agent_runs WHERE agent_run_id = ?", (agent_run_id,)) as cur:
            row = await cur.fetchone()
        return self._run(row) if row else None

    async def find_reusable_run(self, job_id: str, agent_hash: str, input_hash: str) -> AgentRunRow | None:
        async with self.conn.execute(
            """SELECT * FROM agent_runs WHERE job_id = ? AND agent_hash = ? AND input_hash = ?
               AND status = ? AND depth = 1 ORDER BY seq LIMIT 1""",
            (job_id, agent_hash, input_hash, RunStatus.SUCCESS.value),
        ) as cur:
            row = await cur.fetchone()
        return self._run(row) if row else None

    async def list_runs(self, *, job_id: str | None = None, invocation_id: str | None = None) -> list[AgentRunRow]:
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
        sql += " ORDER BY seq"
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._run(r) for r in rows]

    async def _set_run(self, agent_run_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{c} = ?" for c in columns)
        cur = await self.conn.execute(
            f"UPDATE agent_runs SET {assignments} WHERE agent_run_id = ?", (*columns.values(), agent_run_id)
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"No such run: {agent_run_id}")

    @staticmethod
    def _run(row: aiosqlite.Row) -> AgentRunRow:
        data: dict[str, Any] = dict(row)
        data["settings"] = json.loads(data["settings"])
        for column in ("created_at", "started_at", "completed_at"):
            data[column] = datetime.fromisoformat(data[column]) if data[column] else None
        return AgentRunRow(**data)
