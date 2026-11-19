"""`PostgresDatabase`: the run log in Postgres, for a deployment. `SqliteDatabase` is the one for local development.

It implements the same `Database` abstract class with the same records and rows, with native types (timestamptz, jsonb,
boolean, an identity column). The tables live in their own schema (default `featherweight`), so they never collide with an
application's tables. There is no result store for Postgres: where a deployment keeps the outputs that resuming needs is for
the user to decide, so pass `results=` to the runner. Needs the extra `featherweight[postgres]` (`psycopg`, version 3).
"""

import logging
import re
from typing import Any

from featherweight.db import AgentRunRow, Database, JobRow, RunEnd, RunStart, now
from featherweight.errors import SetupError
from featherweight.migrations import MIGRATIONS
from featherweight.model_settings import require
from featherweight.models import RunStatus

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
        try:
            await self._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
            await self._conn.execute(f"SET search_path TO {self.schema}")
            await self._conn.execute("SET TIME ZONE 'UTC'")
            await self._migrate()
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
        if "schema_migrations" not in tables and "agent_runs" in tables:
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

    async def insert_job(self, job_id: str, name: str, resumed_from: str | None, resumable: bool) -> str:
        await self.conn.execute(
            "INSERT INTO jobs (job_id, name, status, resumable, resumed_from, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (job_id, name, RunStatus.INIT.value, resumable, resumed_from, now()),
        )
        logger.info("job_created job_id=%s name=%s resumed_from=%s resumable=%s", job_id, name, resumed_from, resumable)
        return job_id

    async def set_job_status(
        self, job_id: str, status: RunStatus, *, error_type: str | None = None, error_hash: str | None = None
    ) -> None:
        status = RunStatus(status)
        terminal = status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED)
        failed = status is RunStatus.FAILED
        cur = await self.conn.execute(
            "UPDATE jobs SET status = %s, completed_at = %s, error_type = %s, error_hash = %s WHERE job_id = %s",
            (
                status.value,
                now() if terminal else None,
                error_type if failed else None,
                error_hash if failed else None,
                job_id,
            ),
        )
        if cur.rowcount == 0:
            raise LookupError(f"No such job: {job_id}")
        log = logger.warning if failed else logger.info
        log("job_status job_id=%s status=%s error_type=%s", job_id, status.value, error_type if failed else None)

    async def get_job(self, job_id: str) -> JobRow | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))
        row = await cur.fetchone()
        return JobRow(**row) if row else None

    # agent runs

    async def create_run(self, run: RunStart) -> str:
        jsonb = require("psycopg.types.json", "postgres").Jsonb
        await self.conn.execute(
            """INSERT INTO agent_runs (
                   agent_run_id, invocation_id, attempt, job_id, parent_agent_run_id, depth, agent_name, agent_version,
                   agent_hash, system_prompt_hash, tools_hash, provider, model_name, reasoning, settings, input_hash,
                   reused_from_agent_run_id, thread_id, status, created_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                jsonb(run.settings),
                run.input_hash,
                run.reused_from_agent_run_id,
                run.thread_id,
                RunStatus.INIT.value,
                now(),
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
        await self._set_run(agent_run_id, status=RunStatus.RUNNING.value, started_at=now())
        logger.info("run_running agent_run_id=%s", agent_run_id)

    async def finish_run(self, agent_run_id: str, end: RunEnd) -> None:
        await self._set_run(
            agent_run_id,
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
        cur = await self.conn.execute("SELECT * FROM agent_runs WHERE agent_run_id = %s", (agent_run_id,))
        row = await cur.fetchone()
        return AgentRunRow(**row) if row else None

    async def find_reusable_run(self, job_id: str, agent_hash: str, input_hash: str) -> AgentRunRow | None:
        cur = await self.conn.execute(
            """SELECT * FROM agent_runs WHERE job_id = %s AND agent_hash = %s AND input_hash = %s
               AND status = %s AND depth = 1 ORDER BY seq LIMIT 1""",
            (job_id, agent_hash, input_hash, RunStatus.SUCCESS.value),
        )
        row = await cur.fetchone()
        return AgentRunRow(**row) if row else None

    async def list_runs(self, *, job_id: str | None = None, invocation_id: str | None = None) -> list[AgentRunRow]:
        where, params = [], []
        if job_id is not None:
            where.append("job_id = %s")
            params.append(job_id)
        if invocation_id is not None:
            where.append("invocation_id = %s")
            params.append(invocation_id)
        sql = "SELECT * FROM agent_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq"
        cur = await self.conn.execute(sql, params)
        return [AgentRunRow(**row) for row in await cur.fetchall()]

    async def _set_run(self, agent_run_id: str, **columns: Any) -> None:
        assignments = ", ".join(f"{column} = %s" for column in columns)
        cur = await self.conn.execute(
            f"UPDATE agent_runs SET {assignments} WHERE agent_run_id = %s", (*columns.values(), agent_run_id)
        )
        if cur.rowcount == 0:
            raise LookupError(f"No such run: {agent_run_id}")
