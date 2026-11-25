"""`PostgresDatabase`: the run log in Postgres, for a deployment. `SqliteDatabase` is the one for local development.

It implements the same `Database` abstract class with the same records and rows, with native types (timestamptz, jsonb,
boolean, an identity column). The tables live in their own schema (default `featherweight`), so they never collide with an
application's tables. There is no result store for Postgres: where a deployment keeps the outputs that resuming needs is for
the user to decide, so pass `results=` to the runner. Needs the extra `featherweight[postgres]` (`psycopg`, version 3).
"""

import contextlib
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from featherweight.db import (
    FINAL,
    STATUS_EVENT,
    AttemptFinish,
    AttemptRecord,
    AttemptRow,
    Database,
    EventType,
    JobEvent,
    JobLost,
    JobOwner,
    JobRow,
    now,
)
from featherweight.errors import SetupError
from featherweight.migrations import MIGRATIONS
from featherweight.model_settings import require
from featherweight.models import Status

logger = logging.getLogger(__name__)

DIALECT = "postgres"
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresDatabase(Database):
    def __init__(self, dsn: str, schema: str = "featherweight"):
        if not SCHEMA_NAME.match(schema):
            raise ValueError(
                f"The schema {schema!r} must be lowercase letters, digits and underscores, starting with a letter"
            )
        self.dsn = dsn
        self.schema = schema
        self._conn: Any = None

    @property
    def conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first")
        return self._conn

    async def connect(self) -> None:
        psycopg = require("psycopg", "postgres")
        rows = require("psycopg.rows", "postgres")
        self._conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, row_factory=rows.dict_row)
        key = f"featherweight:{self.schema}"
        try:
            # Processes that connect to a fresh database at the same moment (an API and workers started together) would race to make
            # the schema and the tables. One at a time, by an advisory lock on the schema: the others wait, then find it all done.
            await self._conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            try:
                await self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
                await self._conn.execute(f"SET search_path TO {self.schema}")
                await self._conn.execute("SET TIME ZONE 'UTC'")
                await self._migrate()
            finally:  # closing the connection releases it too, so a failure to unlock here is harmless
                with contextlib.suppress(Exception):
                    await self._conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
        except BaseException:
            await self.close()
            raise
        logger.info("db_connected dsn=postgres schema=%s", self.schema)

    async def _tables(self) -> set[str]:
        cur = await self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (self.schema,)
        )
        return {row["table_name"] for row in await cur.fetchall()}

    async def _migrate(self) -> None:
        tables = await self._tables()
        if "schema_migrations" not in tables and "attempts" in tables:
            raise SetupError(
                f"The schema {self.schema!r} is from before migrations (an older version of the run log). Drop it and run "
                "again: nothing is carried over."
            )
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL)"
        )
        cur = await self.conn.execute("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in await cur.fetchall()}
        for migration in sorted(MIGRATIONS, key=lambda m: m.version):
            if migration.version in applied:
                continue
            async with self.conn.transaction():  # a failing migration leaves nothing behind
                await self.conn.execute(migration.sql[DIALECT])
                await self.conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, now()),
                )
            logger.info("db_migrated version=%d name=%s", migration.version, migration.name)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("db_closed schema=%s", self.schema)

    # jobs

    async def _write_event(
        self,
        job_id: str,
        type: EventType,
        status: Status,
        error_type: str | None = None,
        error_hash: str | None = None,
    ) -> JobEvent:
        """Write an event in the transaction that is open. The caller publishes it after the commit."""
        at = now()
        cur = await self.conn.execute(
            "INSERT INTO job_events (job_id, type, status, error_type, error_hash, at) VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING seq",
            (job_id, type, status.value, error_type, error_hash, at),
        )
        row = await cur.fetchone()
        return JobEvent(
            seq=row["seq"], job_id=job_id, type=type, status=status, error_type=error_type, error_hash=error_hash, at=at
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
        parent_attempt_id: str | None = None,
    ) -> str:
        async with self.conn.transaction():  # the job and its event, or neither
            await self.conn.execute(
                "INSERT INTO jobs (job_id, name, status, resumable, resumed_from, job_version, job_hash, input_hash, "
                "parent_attempt_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    name,
                    Status.INIT.value,
                    resumable,
                    resumed_from,
                    job_version,
                    job_hash,
                    input_hash,
                    parent_attempt_id,
                    now(),
                ),
            )
            event = await self._write_event(job_id, "queued", Status.INIT)
        self._publish(event)
        logger.info("job_created job_id=%s name=%s resumed_from=%s resumable=%s", job_id, name, resumed_from, resumable)
        return job_id

    async def set_job_status(
        self,
        job_id: str,
        status: Status,
        *,
        error_type: str | None = None,
        error_hash: str | None = None,
        owner: JobOwner | None = None,
    ) -> None:
        status = Status(status)
        failed = status is Status.FAILED
        started = ", started_at = %s" if status in (Status.RUNNING, Status.INIT) else ""
        extra = ""
        if status is Status.INIT:
            extra = ", worker_id = NULL, lease_expires_at = NULL"  # back in the queue: nobody owns it
        elif status in FINAL:
            extra = ", lease_expires_at = NULL"
        who = " AND worker_id = %s AND executions = %s AND status = %s" if owner is not None else ""
        async with self.conn.transaction():  # the status and its event, or neither
            cur = await self.conn.execute(
                f"UPDATE jobs SET status = %s, completed_at = %s, error_type = %s, error_hash = %s{started}{extra} "
                f"WHERE job_id = %s{who}",
                (
                    status.value,
                    now() if status in FINAL else None,
                    error_type if failed else None,
                    error_hash if failed else None,
                    *([now() if status is Status.RUNNING else None] if started else []),
                    job_id,
                    *([owner.worker_id, owner.executions, Status.RUNNING.value] if owner is not None else []),
                ),
            )
            if cur.rowcount == 0:
                exists = await (await self.conn.execute("SELECT 1 FROM jobs WHERE job_id = %s", (job_id,))).fetchone()
                if exists is None:
                    raise LookupError(f"No such job: {job_id}")
                raise JobLost(f"Job {job_id} is not owned by {owner} any more")
            event = await self._write_event(
                job_id, STATUS_EVENT[status], status, error_type if failed else None, error_hash if failed else None
            )
        self._publish(event)
        log = logger.warning if failed else logger.info
        log("job_status job_id=%s status=%s error_type=%s", job_id, status.value, error_type if failed else None)

    async def request_job_cancel(self, job_id: str) -> bool:
        async with self.conn.transaction():
            cur = await self.conn.execute(
                "SELECT status, cancel_requested FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,)
            )
            row = await cur.fetchone()
            if row is None or Status(row["status"]) in FINAL:
                return False
            if row["cancel_requested"]:
                return True  # asked already: nothing changes, no second event
            await self.conn.execute("UPDATE jobs SET cancel_requested = true WHERE job_id = %s", (job_id,))
            event = await self._write_event(job_id, "cancel_requested", Status(row["status"]))
        self._publish(event)
        logger.info("job_cancel_requested job_id=%s", job_id)
        return True

    async def start_job(self, job_id: str, worker_id: str | None = None, lease: float | None = None) -> bool:
        # The lease ends by the database's own clock, so that workers on different machines agree on when it does.
        expires = "now() + make_interval(secs => %s)" if lease is not None else "NULL"
        return await self._take(
            job_id,
            "UPDATE jobs SET status = %s, started_at = %s, completed_at = NULL, error_type = NULL, error_hash = NULL, "
            f"worker_id = %s, lease_expires_at = {expires}, executions = executions + 1 "
            "WHERE job_id = %s AND status = %s AND NOT cancel_requested",
            (
                Status.RUNNING.value,
                now(),
                worker_id,
                *([lease] if lease is not None else []),
                job_id,
                Status.INIT.value,
            ),
            "running",
            Status.RUNNING,
        )

    async def take_over_job(self, job_id: str, worker_id: str, lease: float) -> bool:
        # The expiry is compared with the database's own clock, which also wrote it, so workers on different machines agree.
        async with self.conn.transaction():
            cur = await self.conn.execute(
                "UPDATE jobs SET started_at = %s, completed_at = NULL, error_type = NULL, error_hash = NULL, worker_id = %s, "
                "lease_expires_at = now() + make_interval(secs => %s), executions = executions + 1 "
                "WHERE job_id = %s AND status = %s AND lease_expires_at IS NOT NULL AND lease_expires_at < now()",
                (now(), worker_id, lease, job_id, Status.RUNNING.value),
            )
            if cur.rowcount == 0:
                return False
            lost = await self._write_event(job_id, "lost", Status.INIT)  # the worker was lost: back in the queue ...
            running = await self._write_event(job_id, "running", Status.RUNNING)  # ... and taken, at once
        self._publish(lost)
        self._publish(running)
        logger.warning("job_taken_over job_id=%s worker_id=%s", job_id, worker_id)
        return True

    async def fail_queued_job(self, job_id: str, error_type: str, error_hash: str) -> bool:
        return await self._take(
            job_id,
            "UPDATE jobs SET status = %s, completed_at = %s, error_type = %s, error_hash = %s, lease_expires_at = NULL "
            "WHERE job_id = %s AND (status = %s OR (status = %s AND lease_expires_at IS NOT NULL AND lease_expires_at < now()))",
            (
                Status.FAILED.value,
                now(),
                error_type,
                error_hash,
                job_id,
                Status.INIT.value,
                Status.RUNNING.value,
            ),
            "failed",
            Status.FAILED,
            error_type,
            error_hash,
        )

    async def renew_job_leases(self, worker_id: str, jobs: Mapping[str, int], lease: float) -> set[str]:
        if not jobs:
            return set()
        pairs = ", ".join("(%s, %s)" for _ in jobs)
        held = [value for job_id, executions in jobs.items() for value in (job_id, executions)]
        async with self.conn.transaction():
            cur = await self.conn.execute(
                "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE worker_id = %s AND status = %s "
                f"AND (job_id, executions) IN (VALUES {pairs}) RETURNING job_id",
                [lease, worker_id, Status.RUNNING.value, *held],
            )
            return {row["job_id"] for row in await cur.fetchall()}

    async def owns_job(self, job_id: str, owner: JobOwner) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = %s AND worker_id = %s AND executions = %s AND status = %s",
            (job_id, owner.worker_id, owner.executions, Status.RUNNING.value),
        )
        return await cur.fetchone() is not None

    async def cancel_queued_job(self, job_id: str) -> bool:
        return await self._take(
            job_id,
            "UPDATE jobs SET status = %s, completed_at = %s WHERE job_id = %s AND status = %s",
            (Status.CANCELLED.value, now(), job_id, Status.INIT.value),
            "cancelled",
            Status.CANCELLED,
        )

    async def _take(
        self,
        job_id: str,
        sql: str,
        params: tuple[Any, ...],
        type: EventType,
        status: Status,
        error_type: str | None = None,
        error_hash: str | None = None,
    ) -> bool:
        """A compare-and-set on a job: the update has the old status in its `WHERE`, and the event is written in the same
        transaction only if the update changed a row."""
        async with self.conn.transaction():
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
        cur = await self.conn.execute(
            "SELECT job_id FROM jobs WHERE job_id = ANY(%s) AND cancel_requested AND status <> ALL(%s)",
            (list(job_ids), [status.value for status in FINAL]),
        )
        return {row["job_id"] for row in await cur.fetchall()}

    async def get_job(self, job_id: str) -> JobRow | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
        row = await cur.fetchone()
        return JobRow(**row) if row else None

    # job events

    async def list_job_events(self, job_id: str, after: int = 0, limit: int | None = None) -> list[JobEvent]:
        sql = "SELECT * FROM job_events WHERE job_id = %s AND seq > %s ORDER BY seq"
        params: list[Any] = [job_id, after]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        cur = await self.conn.execute(sql, params)
        return [JobEvent(**row) for row in await cur.fetchall()]

    async def poll_job_events(self, job_ids: Sequence[str], after: int = 0) -> list[JobEvent]:
        if not job_ids:
            return []
        cur = await self.conn.execute(
            "SELECT * FROM job_events WHERE job_id = ANY(%s) AND seq > %s ORDER BY seq", (list(job_ids), after)
        )
        return [JobEvent(**row) for row in await cur.fetchall()]

    # attempts

    async def create_attempt(self, run: AttemptRecord) -> str:
        jsonb = require("psycopg.types.json", "postgres").Jsonb
        await self.conn.execute(
            """INSERT INTO attempts (
                   attempt_id, run_id, attempt, job_id, parent_attempt_id, depth, agent_name, agent_version,
                   agent_hash, system_prompt_hash, tools_hash, provider, model_name, reasoning, settings, input_hash,
                   identity_hash, reused_from_attempt_id, job_execution, thread_id, status, created_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         COALESCE(%s, (SELECT executions FROM jobs WHERE job_id = %s)), %s, %s, %s)""",
            (
                run.attempt_id,
                run.run_id,
                run.attempt,
                run.job_id,
                run.parent_attempt_id,
                run.depth,
                run.agent_name,
                run.agent_version,
                run.agent_hash,
                run.system_prompt_hash,
                run.tools_hash,
                run.provider,
                run.model_name,
                run.reasoning,
                jsonb(run.settings),
                run.input_hash,
                run.identity_hash,
                run.reused_from_attempt_id,
                run.job_execution,
                run.job_id,  # no execution given: the job's `executions` now
                run.thread_id,
                Status.INIT.value,
                now(),
            ),
        )
        logger.info(
            "attempt_created attempt_id=%s run_id=%s attempt=%d job_id=%s agent=%s version=%s model=%s "
            "parent_attempt_id=%s depth=%d",
            run.attempt_id,
            run.run_id,
            run.attempt,
            run.job_id,
            run.agent_name,
            run.agent_version,
            run.model_name,
            run.parent_attempt_id,
            run.depth,
        )
        return run.attempt_id

    async def start_attempt(self, attempt_id: str) -> None:
        await self._set_run(attempt_id, status=Status.RUNNING.value, started_at=now())
        logger.info("attempt_running attempt_id=%s", attempt_id)

    async def finish_attempt(self, attempt_id: str, end: AttemptFinish) -> None:
        await self._set_run(
            attempt_id,
            status=end.status.value,
            completed_at=now(),
            output_hash=end.output_hash,
            error_type=end.error_type,
            error_hash=end.error_hash,
            input_tokens=end.input_tokens,
            output_tokens=end.output_tokens,
            latency_ms=end.latency_ms,
            reported_model_name=end.reported_model_name,
        )
        log = logger.warning if end.status is Status.FAILED else logger.info
        log(
            "attempt_finished attempt_id=%s status=%s input_tokens=%s output_tokens=%s latency_ms=%s error_type=%s",
            attempt_id,
            end.status.value,
            end.input_tokens,
            end.output_tokens,
            end.latency_ms,
            end.error_type,
        )

    async def get_attempt(self, attempt_id: str) -> AttemptRow | None:
        cur = await self.conn.execute("SELECT * FROM attempts WHERE attempt_id = %s", (attempt_id,))
        row = await cur.fetchone()
        return AttemptRow(**row) if row else None

    async def job_has_other_identity(self, job_id: str, identity_hash: str | None) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM attempts WHERE job_id = %s AND identity_hash IS DISTINCT FROM %s LIMIT 1",
            (job_id, identity_hash),
        )
        return await cur.fetchone() is not None

    async def find_reusable_attempt(
        self,
        job_id: str,
        agent_hash: str,
        input_hash: str,
        identity_hash: str | None,
        before_execution: int | None = None,
    ) -> AttemptRow | None:
        bound = " AND job_execution < %s" if before_execution is not None else ""
        cur = await self.conn.execute(
            f"""SELECT * FROM attempts WHERE job_id = %s AND agent_hash = %s AND input_hash = %s
               AND identity_hash IS NOT DISTINCT FROM %s AND status = %s AND depth = 1{bound} ORDER BY seq LIMIT 1""",
            (
                job_id,
                agent_hash,
                input_hash,
                identity_hash,
                Status.SUCCESS.value,
                *([before_execution] if bound else []),
            ),
        )
        row = await cur.fetchone()
        return AttemptRow(**row) if row else None

    async def list_attempts(self, *, job_id: str | None = None, run_id: str | None = None) -> list[AttemptRow]:
        where, params = [], []
        if job_id is not None:
            where.append("job_id = %s")
            params.append(job_id)
        if run_id is not None:
            where.append("run_id = %s")
            params.append(run_id)
        sql = "SELECT * FROM attempts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq"
        cur = await self.conn.execute(sql, params)
        return [AttemptRow(**row) for row in await cur.fetchall()]

    async def _set_run(self, attempt_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{column} = %s" for column in columns)
        cur = await self.conn.execute(
            f"UPDATE attempts SET {assignments} WHERE attempt_id = %s", (*columns.values(), attempt_id)
        )
        if cur.rowcount == 0:
            raise LookupError(f"No such run: {attempt_id}")
