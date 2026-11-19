"""The schema, as numbered migrations.

Each migration has the SQL of each dialect that has a database implementation. `connect()` applies the pending ones in order,
each in a transaction, and records them in `schema_migrations`. A migration is never edited once it is released; a change is a
new migration. The baseline is the whole schema: nothing was released before it, so no data is carried over.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

STATUSES = "('INIT', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED')"


class Migration(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    name: str
    sql: Mapping[str, str]  # by dialect


SQLITE_BASELINE = f"""
CREATE TABLE jobs (
    job_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN {STATUSES}),
    resumable     INTEGER NOT NULL DEFAULT 0 CHECK (resumable IN (0, 1)),
    resumed_from  TEXT REFERENCES jobs(job_id),
    error_type    TEXT,
    error_hash    TEXT,
    created_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE TABLE agent_runs (
    seq                       INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_run_id              TEXT NOT NULL UNIQUE,
    invocation_id             TEXT NOT NULL,
    attempt                   INTEGER NOT NULL CHECK (attempt >= 1),
    job_id                    TEXT REFERENCES jobs(job_id),
    parent_agent_run_id       TEXT,
    depth                     INTEGER NOT NULL CHECK (depth >= 1),
    agent_name                TEXT NOT NULL,
    agent_version             TEXT NOT NULL,
    agent_hash                TEXT NOT NULL,
    system_prompt_hash        TEXT NOT NULL,
    tools_hash                TEXT NOT NULL,
    provider                  TEXT NOT NULL,
    model_name                TEXT NOT NULL,
    reported_model_name       TEXT,
    reasoning                 TEXT NOT NULL,
    settings                  TEXT NOT NULL,
    input_hash                TEXT NOT NULL,
    output_hash               TEXT,
    reused_from_agent_run_id  TEXT,
    thread_id                 TEXT,
    status                    TEXT NOT NULL CHECK (status IN {STATUSES}),
    error_type                TEXT,
    error_hash                TEXT,
    input_tokens              INTEGER CHECK (input_tokens >= 0),
    output_tokens             INTEGER CHECK (output_tokens >= 0),
    latency_ms                REAL CHECK (latency_ms >= 0),
    created_at                TEXT NOT NULL,
    started_at                TEXT,
    completed_at              TEXT
);

CREATE INDEX idx_agent_runs_job ON agent_runs(job_id);
CREATE INDEX idx_agent_runs_invocation ON agent_runs(invocation_id);
CREATE INDEX idx_agent_runs_parent ON agent_runs(parent_agent_run_id);
CREATE INDEX idx_agent_runs_reuse ON agent_runs(job_id, agent_hash, input_hash);

CREATE TABLE results (
    job_id       TEXT NOT NULL,
    agent_hash   TEXT NOT NULL,
    input_hash   TEXT NOT NULL,
    output       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (job_id, agent_hash, input_hash)
);
"""

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="baseline", sql={"sqlite": SQLITE_BASELINE}),
]
