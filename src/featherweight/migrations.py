"""The schema, as numbered migrations.

Each migration has the SQL of each dialect that has a database implementation. `connect()` applies the pending ones in order,
each in a transaction, and records them in `schema_migrations`. A migration is never edited once it is released; a change is a
new migration. The baseline is the whole schema: nothing was released before it, so no data is carried over.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

STATUSES = "('INIT', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED')"
EVENT_TYPES = "('queued', 'running', 'succeeded', 'failed', 'cancelled', 'cancel_requested', 'lost')"


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
    parent_agent_run_id TEXT,
    job_version   TEXT,
    job_hash      TEXT,
    input_hash    TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    worker_id     TEXT,
    lease_expires_at TEXT,
    executions    INTEGER NOT NULL DEFAULT 0 CHECK (executions >= 0),
    error_type    TEXT,
    error_hash    TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
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
    job_execution             INTEGER,
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

CREATE TABLE job_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    type        TEXT NOT NULL CHECK (type IN {EVENT_TYPES}),
    status      TEXT NOT NULL CHECK (status IN {STATUSES}),
    error_type  TEXT,
    error_hash  TEXT,
    at          TEXT NOT NULL
);

CREATE INDEX idx_job_events_job ON job_events(job_id, seq);

CREATE TABLE results (
    job_id       TEXT NOT NULL,
    agent_hash   TEXT NOT NULL,
    input_hash   TEXT NOT NULL,
    output       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (job_id, agent_hash, input_hash)
);

CREATE TABLE job_queue (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    job_name     TEXT NOT NULL,
    input        TEXT NOT NULL,
    trace_context TEXT NOT NULL DEFAULT '{{}}',
    state        TEXT NOT NULL CHECK (state IN ('ready', 'claimed', 'dead')),
    enqueued_at  TEXT NOT NULL,
    claimed_at   TEXT,
    lease_expires_at TEXT,
    delivery_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_job_queue_state ON job_queue(state, seq);
"""

# Postgres has native types (timestamptz, jsonb, boolean, an identity column). There is no `results` table: we ship no Postgres
# result store (the user writes one), so a deployment decides where its results live.
POSTGRES_BASELINE = f"""
CREATE TABLE jobs (
    job_id        text PRIMARY KEY,
    name          text NOT NULL,
    status        text NOT NULL CHECK (status IN {STATUSES}),
    resumable     boolean NOT NULL DEFAULT false,
    resumed_from  text REFERENCES jobs(job_id),
    parent_agent_run_id text,
    job_version   text,
    job_hash      text,
    input_hash    text,
    cancel_requested boolean NOT NULL DEFAULT false,
    worker_id     text,
    lease_expires_at timestamptz,
    executions    integer NOT NULL DEFAULT 0 CHECK (executions >= 0),
    error_type    text,
    error_hash    text,
    created_at    timestamptz NOT NULL,
    started_at    timestamptz,
    completed_at  timestamptz
);

CREATE TABLE agent_runs (
    seq                       bigint GENERATED ALWAYS AS IDENTITY,
    agent_run_id              text PRIMARY KEY,
    invocation_id             text NOT NULL,
    attempt                   integer NOT NULL CHECK (attempt >= 1),
    job_id                    text REFERENCES jobs(job_id),
    parent_agent_run_id       text,
    depth                     integer NOT NULL CHECK (depth >= 1),
    agent_name                text NOT NULL,
    agent_version             text NOT NULL,
    agent_hash                text NOT NULL,
    system_prompt_hash        text NOT NULL,
    tools_hash                text NOT NULL,
    provider                  text NOT NULL,
    model_name                text NOT NULL,
    reported_model_name       text,
    reasoning                 text NOT NULL,
    settings                  jsonb NOT NULL,
    input_hash                text NOT NULL,
    output_hash               text,
    reused_from_agent_run_id  text,
    job_execution             integer,
    thread_id                 text,
    status                    text NOT NULL CHECK (status IN {STATUSES}),
    error_type                text,
    error_hash                text,
    input_tokens              integer CHECK (input_tokens >= 0),
    output_tokens             integer CHECK (output_tokens >= 0),
    latency_ms                double precision CHECK (latency_ms >= 0),
    created_at                timestamptz NOT NULL,
    started_at                timestamptz,
    completed_at              timestamptz
);

CREATE UNIQUE INDEX idx_agent_runs_seq ON agent_runs(seq);
CREATE INDEX idx_agent_runs_job ON agent_runs(job_id);
CREATE INDEX idx_agent_runs_invocation ON agent_runs(invocation_id);
CREATE INDEX idx_agent_runs_parent ON agent_runs(parent_agent_run_id);
CREATE INDEX idx_agent_runs_reuse ON agent_runs(job_id, agent_hash, input_hash);

CREATE TABLE job_events (
    seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      text NOT NULL REFERENCES jobs(job_id),
    type        text NOT NULL CHECK (type IN {EVENT_TYPES}),
    status      text NOT NULL CHECK (status IN {STATUSES}),
    error_type  text,
    error_hash  text,
    at          timestamptz NOT NULL
);

CREATE INDEX idx_job_events_job ON job_events(job_id, seq);
"""

# The hash of the identity of a run, so that a resume reuses only a step of the same identity (docs/specs/identity.md). NULL: the run had none,
# and so does every run from before this column.
SQLITE_IDENTITY_HASH = "ALTER TABLE agent_runs ADD COLUMN identity_hash TEXT;"
POSTGRES_IDENTITY_HASH = "ALTER TABLE agent_runs ADD COLUMN identity_hash text;"

# The identity of the run in a queue message (docs/specs/identity.md). The queue table is sqlite's (we ship no Postgres queue), so Postgres has nothing to do.
SQLITE_QUEUE_IDENTITY = "ALTER TABLE job_queue ADD COLUMN identity TEXT;"
POSTGRES_NOTHING = "SELECT 1;"

# The run log's rows are attempts, and a run has an id of its own (docs/specs/units.md): the table, its columns and its indexes say so. The rows are kept.
SQLITE_ATTEMPTS = """
ALTER TABLE agent_runs RENAME TO attempts;
ALTER TABLE attempts RENAME COLUMN agent_run_id TO attempt_id;
ALTER TABLE attempts RENAME COLUMN invocation_id TO run_id;
ALTER TABLE attempts RENAME COLUMN parent_agent_run_id TO parent_attempt_id;
ALTER TABLE attempts RENAME COLUMN reused_from_agent_run_id TO reused_from_attempt_id;
ALTER TABLE jobs RENAME COLUMN parent_agent_run_id TO parent_attempt_id;
DROP INDEX idx_agent_runs_job;
DROP INDEX idx_agent_runs_invocation;
DROP INDEX idx_agent_runs_parent;
DROP INDEX idx_agent_runs_reuse;
CREATE INDEX idx_attempts_job ON attempts(job_id);
CREATE INDEX idx_attempts_run ON attempts(run_id);
CREATE INDEX idx_attempts_parent ON attempts(parent_attempt_id);
CREATE INDEX idx_attempts_reuse ON attempts(job_id, agent_hash, input_hash);
"""
POSTGRES_ATTEMPTS = """
ALTER TABLE agent_runs RENAME TO attempts;
ALTER TABLE attempts RENAME COLUMN agent_run_id TO attempt_id;
ALTER TABLE attempts RENAME COLUMN invocation_id TO run_id;
ALTER TABLE attempts RENAME COLUMN parent_agent_run_id TO parent_attempt_id;
ALTER TABLE attempts RENAME COLUMN reused_from_agent_run_id TO reused_from_attempt_id;
ALTER TABLE jobs RENAME COLUMN parent_agent_run_id TO parent_attempt_id;
ALTER INDEX idx_agent_runs_seq RENAME TO idx_attempts_seq;
ALTER INDEX idx_agent_runs_job RENAME TO idx_attempts_job;
ALTER INDEX idx_agent_runs_invocation RENAME TO idx_attempts_run;
ALTER INDEX idx_agent_runs_parent RENAME TO idx_attempts_parent;
ALTER INDEX idx_agent_runs_reuse RENAME TO idx_attempts_reuse;
ALTER SEQUENCE IF EXISTS agent_runs_seq_seq RENAME TO attempts_seq_seq;
"""

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="baseline", sql={"sqlite": SQLITE_BASELINE, "postgres": POSTGRES_BASELINE}),
    Migration(
        version=2,
        name="identity_hash",
        sql={"sqlite": SQLITE_IDENTITY_HASH, "postgres": POSTGRES_IDENTITY_HASH},
    ),
    Migration(version=3, name="queue_identity", sql={"sqlite": SQLITE_QUEUE_IDENTITY, "postgres": POSTGRES_NOTHING}),
    Migration(version=4, name="attempts", sql={"sqlite": SQLITE_ATTEMPTS, "postgres": POSTGRES_ATTEMPTS}),
]
