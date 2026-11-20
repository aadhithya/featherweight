<!-- nav:top -->
[Book](../README.md) › [Part 6: Bring your own storage](../README.md#part-6-bring-your-own-storage) › A result store  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# A result store

Keep the results of jobs where you want them.

A resumed job and `client.result` read results from a `ResultStore`. It has three methods: `put`, `get` and `delete_job`. The key is the job id, the hash
of the agent or job, and the hash of the input; the output is a JSON `dict`.

The result stores are `SqliteResultStore` (the default: a table in the run log's file, for local work) and yours, over S3, Redis or a table:

<!-- code: code/6-2/app.py -->
```python
"""Chapter 6-2: a result store of your own, checked with the kit. No model, no key."""

import asyncio
import json
import tempfile
from pathlib import Path

from featherweight import ResultKey, ResultStore, testing


class FileResultStore(ResultStore):
    """One JSON file for each result, in a folder for each job. Stores: SqliteResultStore (the default, local) or yours: S3, Redis, a table."""

    def __init__(self, folder: Path):
        self.folder = folder

    def _file(self, key: ResultKey) -> Path:
        return self.folder / key.job_id / f"{key.agent_hash}-{key.input_hash}.json"

    async def put(self, key: ResultKey, output: dict) -> None:
        self._file(key).parent.mkdir(parents=True, exist_ok=True)
        self._file(key).write_text(json.dumps(output))

    async def get(self, key: ResultKey) -> dict | None:
        file = self._file(key)
        return json.loads(file.read_text()) if file.exists() else None

    async def delete_job(self, job_id: str) -> None:
        for file in (self.folder / job_id).glob("*.json"):
            file.unlink()


async def main() -> None:
    await testing.check_result_store(lambda: FileResultStore(Path(tempfile.mkdtemp())))  # raises if a rule is broken
    print("FileResultStore follows the ResultStore contract")


if __name__ == "__main__":
    asyncio.run(main())
```

```text
make book-chapter N=6-2
FileResultStore follows the ResultStore contract
```

It needs no model and no key. Use it with `AgentRunner(db, results=FileResultStore(Path("results")))`.

## What to notice

- `testing.check_result_store` takes a function that makes a fresh, empty store, and raises an `AssertionError` that names the rule your store breaks.
- The store never sees featherweight's tables: it holds only the outputs you decided to keep.

> **Why** featherweight keeps no results itself: they are your data, and how they are stored is your call. See [the result store](../../specs/persistence.md#the-result-store).

<!-- nav:bottom -->
---

[← Postgres](01-postgres.md) · [Part 6: Bring your own storage](../README.md#part-6-bring-your-own-storage) · [A job queue →](03-a-job-queue.md)
<!-- /nav:bottom -->
