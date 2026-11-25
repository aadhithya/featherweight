# A result store of your own

Resuming a job reads the outputs of finished steps from a result store. Write one for your own storage: subclass `ResultStore`, write `put`, `get` and `delete_job`, and check it with the conformance kit.

```python
import asyncio

from featherweight import AgentRunner, ResultKey, ResultStore, SqliteDatabase, default_db_path, testing


class MyStore(ResultStore):
    """Results in a dict, to show the shape: yours writes to a database, S3 or a file."""

    def __init__(self) -> None:
        self.results: dict[ResultKey, dict] = {}

    async def put(self, key: ResultKey, output: dict) -> None:  # key: job_id, agent_hash, input_hash
        self.results[key] = output

    async def get(self, key: ResultKey) -> dict | None:
        return self.results.get(key)

    async def delete_job(self, job_id: str) -> None:
        for key in [k for k in self.results if k.job_id == job_id]:
            del self.results[key]


async def main() -> None:
    await testing.check_result_store(
        lambda: MyStore()
    )  # a missing method cannot be built; a broken rule raises AssertionError
    async with SqliteDatabase(default_db_path()) as db:
        runner = AgentRunner(db, results=MyStore())  # use it where jobs run and resume
        print("results kept by", type(runner.results).__name__)


if __name__ == "__main__":
    asyncio.run(main())
```

The first job has to be created `resumable=True`. `examples/results/store.py` is a whole store (a JSON file per result).
