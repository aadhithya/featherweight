<!-- nav:top -->
[Book](../README.md) › [Part 6: Bring your own storage](../README.md#part-6-bring-your-own-storage) › Postgres
<!-- /nav:top -->

# Postgres

Switch the run log to Postgres.

The run log is a `Database`. `SqliteDatabase` is for local work and `PostgresDatabase` is for a deployment. They share one interface, so
one line changes. The runner, the jobs and the agents do not.

The book's container starts no database and does not install the Postgres driver, so run this chapter in a venv on your machine:

```text
pip install 'featherweight[postgres]'
docker run --rm -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16-alpine
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres python 6-1/app.py
```

<!-- code: code/6-1/app.py -->
```python
"""Chapter 6-1: the same run log, in Postgres. One line changes."""

import asyncio
import os

from featherweight import PostgresDatabase

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")


async def main() -> None:
    # Databases: SqliteDatabase (local), PostgresDatabase (a deployment), or your own (Chapter 6-4). The runner does not change.
    async with PostgresDatabase(DSN) as db:  # it was SqliteDatabase(...): the tables are made in a schema of their own
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        job = await db.get_job(job_id)
        print(job.status.value if job else "no job")  # INIT


if __name__ == "__main__":
    asyncio.run(main())
```

```text
INIT
```

## What to notice

- The databases are `SqliteDatabase` (local), `PostgresDatabase` (a deployment) and your own ([a database](04-a-database.md)).
- The tables go in a schema of their own (`featherweight` by default), made by the same numbered migrations as in sqlite.
- Two things stay sqlite: `SqliteQueue` and the default result store need the sqlite database. With Postgres, bring a queue ([a job queue](03-a-job-queue.md))
  and pass a result store (`AgentRunner(db, results=...)`: [a result store](02-a-result-store.md)).

> **Why** there is no Postgres result store: where results live is your decision, so featherweight asks you for one. See [persistence](../../specs/persistence.md#postgres).

<!-- nav:bottom -->
---

[← Observability](../05-in-production/04-observability.md) · [Part 6: Bring your own storage](../README.md#part-6-bring-your-own-storage) · [A result store →](02-a-result-store.md)
<!-- /nav:bottom -->
