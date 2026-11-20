"""`SqliteDatabase`: the run log in a sqlite file. For local development; deployments use `PostgresDatabase`."""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from featherweight.db import (
    FINAL,
    STATUS_EVENT,
    AgentRunRow,
    Database,
    EventType,
    JobEvent,
    JobLost,
    JobOwner,
    JobRow,
    RunEnd,
    RunStart,
    now,
)
from featherweight.errors import SetupError
from featherweight.migrations import MIGRATIONS
from featherweight.models import RunStatus

logger = logging.getLogger(__name__)

DIALECT = "sqlite"


def _stamp(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


class SqliteDatabase(Database):
    def __init__(self, path: str | Path):
        """`path` is the sqlite file, or `":memory:"` for a database that lives in memory (no file, gone when it closes)."""
        self.path = Path(path)
        self.in_memory = str(path) == ":memory:"
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()  # one write transaction at a time: they share one connection

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection]:
        """One write transaction at a time on the connection, committed at the end and rolled back on any error. Everything
        that writes (the run log, the result store, the queue) goes through it, so that two tasks writing at once cannot
        commit or roll back each other's half-done work."""
        async with self._lock:
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def connect(self) -> None:
        if not self.in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(":memory:" if self.in_memory else self.path)
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

    async def _write_event(
        self,
        job_id: str,
        type: EventType,
        status: RunStatus,
        error_type: str | None = None,
        error_hash: str | None = None,
    ) -> JobEvent:
        """Write an event in the transaction that is open. The caller commits, and publishes the event after."""
        at = now()
        cur = await self.conn.execute(
            "INSERT INTO job_events (job_id, type, status, error_type, error_hash, at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, type, status.value, error_type, error_hash, at.isoformat()),
        )
        return JobEvent(
            seq=cur.lastrowid or 0,
            job_id=job_id,
            type=type,
            status=status,
            error_type=error_type,
            error_hash=error_hash,
            at=at,
        )

    async def insert_job(
        self,
        job_id: str,
        name: str,
        resumed_from: str | None,
        resumable: bool,
        job_version: str | None,
        job_hash: str | None,
        input_hash: str | None,
    ) -> str:
        async with self.transaction():
            await self.conn.execute(
                "INSERT INTO jobs (job_id, name, status, resumable, resumed_from, job_version, job_hash, input_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    name,
                    RunStatus.INIT.value,
                    int(resumable),
                    resumed_from,
                    job_version,
                    job_hash,
                    input_hash,
                    now().isoformat(),
                ),
            )
            event = await self._write_event(job_id, "queued", RunStatus.INIT)
        self._publish(event)
        logger.info("job_created job_id=%s name=%s resumed_from=%s resumable=%s", job_id, name, resumed_from, resumable)
        return job_id

    async def set_job_status(
        self,
        job_id: str,
        status: RunStatus,
        *,
        error_type: str | None = None,
        error_hash: str | None = None,
        owner: JobOwner | None = None,
    ) -> None:
        status = RunStatus(status)
        failed = status is RunStatus.FAILED
        started = ", started_at = ?" if status in (RunStatus.RUNNING, RunStatus.INIT) else ""
        if status is RunStatus.INIT:
            started += ", worker_id = NULL, lease_expires_at = NULL"  # back in the queue: nobody owns it
        elif status in FINAL:
            started += ", lease_expires_at = NULL"
        who = " AND worker_id = ? AND executions = ? AND status = ?" if owner is not None else ""
        async with self.transaction():
            cur = await self.conn.execute(
                f"UPDATE jobs SET status = ?, completed_at = ?, error_type = ?, error_hash = ?{started} WHERE job_id = ?{who}",
                (
                    status.value,
                    now().isoformat() if status in FINAL else None,
                    error_type if failed else None,
                    error_hash if failed else None,
                    *([now().isoformat() if status is RunStatus.RUNNING else None] if "started_at" in started else []),
                    job_id,
                    *([owner.worker_id, owner.executions, RunStatus.RUNNING.value] if owner is not None else []),
                ),
            )
            if cur.rowcount == 0:
                async with self.conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)) as exists:
                    if await exists.fetchone() is None:
                        raise LookupError(f"No such job: {job_id}")
                raise JobLost(f"Job {job_id} is not owned by {owner} any more")
            event = await self._write_event(
                job_id, STATUS_EVENT[status], status, error_type if failed else None, error_hash if failed else None
            )
        self._publish(event)
        log = logger.warning if failed else logger.info
        log("job_status job_id=%s status=%s error_type=%s", job_id, status.value, error_type if failed else None)

    async def request_job_cancel(self, job_id: str) -> bool:
        async with self.transaction():
            async with self.conn.execute(
                "SELECT status, cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None or RunStatus(row["status"]) in FINAL:
                return False
            if row["cancel_requested"]:
                return True  # asked already: nothing changes, no second event
            await self.conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE job_id = ?", (job_id,))
            event = await self._write_event(job_id, "cancel_requested", RunStatus(row["status"]))
        self._publish(event)
        logger.info("job_cancel_requested job_id=%s", job_id)
        return True

    async def start_job(self, job_id: str, worker_id: str | None = None, lease: float | None = None) -> bool:
        started = now()
        expires = (started + timedelta(seconds=lease)).isoformat() if lease is not None else None
        return await self._take(
            job_id,
            "UPDATE jobs SET status = ?, started_at = ?, completed_at = NULL, error_type = NULL, error_hash = NULL, "
            "worker_id = ?, lease_expires_at = ?, executions = executions + 1 "
            "WHERE job_id = ? AND status = ? AND cancel_requested = 0",
            (RunStatus.RUNNING.value, started.isoformat(), worker_id, expires, job_id, RunStatus.INIT.value),
            "running",
            RunStatus.RUNNING,
        )

    async def take_over_job(self, job_id: str, worker_id: str, lease: float) -> bool:
        moment = now()
        async with self.transaction():
            cur = await self.conn.execute(
                "UPDATE jobs SET started_at = ?, completed_at = NULL, error_type = NULL, error_hash = NULL, worker_id = ?, "
                "lease_expires_at = ?, executions = executions + 1 "
                "WHERE job_id = ? AND status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (
                    moment.isoformat(),
                    worker_id,
                    (moment + timedelta(seconds=lease)).isoformat(),
                    job_id,
                    RunStatus.RUNNING.value,
                    moment.isoformat(),
                ),
            )
            if cur.rowcount == 0:
                return False
            lost = await self._write_event(job_id, "lost", RunStatus.INIT)  # the worker was lost: back in the queue ...
            running = await self._write_event(job_id, "running", RunStatus.RUNNING)  # ... and taken, at once
        self._publish(lost)
        self._publish(running)
        logger.warning("job_taken_over job_id=%s worker_id=%s", job_id, worker_id)
        return True

    async def fail_queued_job(self, job_id: str, error_type: str, error_hash: str) -> bool:
        return await self._take(
            job_id,
            "UPDATE jobs SET status = ?, completed_at = ?, error_type = ?, error_hash = ?, lease_expires_at = NULL "
            "WHERE job_id = ? AND (status = ? OR (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?))",
            (
                RunStatus.FAILED.value,
                now().isoformat(),
                error_type,
                error_hash,
                job_id,
                RunStatus.INIT.value,
                RunStatus.RUNNING.value,
                now().isoformat(),
            ),
            "failed",
            RunStatus.FAILED,
            error_type,
            error_hash,
        )

    async def renew_job_leases(self, worker_id: str, jobs: Mapping[str, int], lease: float) -> set[str]:
        if not jobs:
            return set()
        expires = (now() + timedelta(seconds=lease)).isoformat()
        pairs = ", ".join("(?, ?)" for _ in jobs)
        held = [value for job_id, executions in jobs.items() for value in (job_id, executions)]
        async with self.transaction() as conn:
            cur = await conn.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE worker_id = ? AND status = ? "
                f"AND (job_id, executions) IN (VALUES {pairs}) RETURNING job_id",
                [expires, worker_id, RunStatus.RUNNING.value, *held],
            )
            return {row["job_id"] for row in await cur.fetchall()}

    async def owns_job(self, job_id: str, owner: JobOwner) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ? AND worker_id = ? AND executions = ? AND status = ?",
            (job_id, owner.worker_id, owner.executions, RunStatus.RUNNING.value),
        ) as cur:
            return await cur.fetchone() is not None

    async def cancel_queued_job(self, job_id: str) -> bool:
        return await self._take(
            job_id,
            "UPDATE jobs SET status = ?, completed_at = ? WHERE job_id = ? AND status = ?",
            (RunStatus.CANCELLED.value, now().isoformat(), job_id, RunStatus.INIT.value),
            "cancelled",
            RunStatus.CANCELLED,
        )

    async def _take(
        self,
        job_id: str,
        sql: str,
        params: tuple[Any, ...],
        type: EventType,
        status: RunStatus,
        error_type: str | None = None,
        error_hash: str | None = None,
    ) -> bool:
        """A compare-and-set on a job: the update has the old status in its `WHERE`, and the event is written in the same
        transaction only if the update changed a row."""
        async with self.transaction():
            cur = await self.conn.execute(sql, params)
            if cur.rowcount == 0:
                return False
            event = await self._write_event(job_id, type, status, error_type, error_hash)
        self._publish(event)
        logger.info("job_status job_id=%s status=%s", job_id, status.value)
        return True

    async def list_cancel_requested(self, job_ids: Sequence[str]) -> set[str]:
        if not job_ids:
            return set()
        marks = ", ".join("?" for _ in job_ids)
        final = [status.value for status in FINAL]
        async with self.conn.execute(
            f"SELECT job_id FROM jobs WHERE job_id IN ({marks}) AND cancel_requested = 1 "
            f"AND status NOT IN ({', '.join('?' for _ in final)})",
            [*job_ids, *final],
        ) as cur:
            return {row["job_id"] for row in await cur.fetchall()}

    async def get_job(self, job_id: str) -> JobRow | None:
        async with self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return self._job(row) if row else None

    # job events

    async def list_job_events(self, job_id: str, after: int = 0, limit: int | None = None) -> list[JobEvent]:
        sql = "SELECT * FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq"
        params: list[Any] = [job_id, after]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        async with self.conn.execute(sql, params) as cur:
            return [self._event(row) for row in await cur.fetchall()]

    async def poll_job_events(self, job_ids: Sequence[str], after: int = 0) -> list[JobEvent]:
        if not job_ids:
            return []
        marks = ", ".join("?" for _ in job_ids)
        async with self.conn.execute(
            f"SELECT * FROM job_events WHERE job_id IN ({marks}) AND seq > ? ORDER BY seq", [*job_ids, after]
        ) as cur:
            return [self._event(row) for row in await cur.fetchall()]

    @staticmethod
    def _event(row: aiosqlite.Row) -> JobEvent:
        data: dict[str, Any] = dict(row)
        data["at"] = datetime.fromisoformat(data["at"])
        return JobEvent(**data)

    @staticmethod
    def _job(row: aiosqlite.Row) -> JobRow:
        data: dict[str, Any] = dict(row)
        data["resumable"] = bool(data["resumable"])
        data["cancel_requested"] = bool(data["cancel_requested"])
        for column in ("created_at", "started_at", "completed_at", "lease_expires_at"):
            data[column] = datetime.fromisoformat(data[column]) if data[column] else None
        return JobRow(**data)

    # agent runs

    async def create_run(self, run: RunStart) -> str:
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO agent_runs (
                       agent_run_id, invocation_id, attempt, job_id, parent_agent_run_id, depth, agent_name, agent_version,
                       agent_hash, system_prompt_hash, tools_hash, provider, model_name, reasoning, settings, input_hash,
                       reused_from_agent_run_id, job_execution, thread_id, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             COALESCE(?, (SELECT executions FROM jobs WHERE job_id = ?)), ?, ?, ?)""",
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
                    run.job_execution,
                    run.job_id,  # no execution given: the job's `executions` now
                    run.thread_id,
                    RunStatus.INIT.value,
                    now().isoformat(),
                ),
            )
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

    async def find_reusable_run(
        self, job_id: str, agent_hash: str, input_hash: str, before_execution: int | None = None
    ) -> AgentRunRow | None:
        bound = " AND job_execution < ?" if before_execution is not None else ""
        async with self.conn.execute(
            f"""SELECT * FROM agent_runs WHERE job_id = ? AND agent_hash = ? AND input_hash = ?
               AND status = ? AND depth = 1{bound} ORDER BY seq LIMIT 1""",
            (job_id, agent_hash, input_hash, RunStatus.SUCCESS.value, *([before_execution] if bound else [])),
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
        async with self.transaction() as conn:
            cur = await conn.execute(
                f"UPDATE agent_runs SET {assignments} WHERE agent_run_id = ?", (*columns.values(), agent_run_id)
            )
        if cur.rowcount == 0:
            raise LookupError(f"No such run: {agent_run_id}")

    @staticmethod
    def _run(row: aiosqlite.Row) -> AgentRunRow:
        data: dict[str, Any] = dict(row)
        data["settings"] = json.loads(data["settings"])
        for column in ("created_at", "started_at", "completed_at"):
            data[column] = datetime.fromisoformat(data[column]) if data[column] else None
        return AgentRunRow(**data)
