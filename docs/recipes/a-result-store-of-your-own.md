# A result store of your own

Resuming a job reads the outputs of finished steps from a `ResultStore`. Subclass it, write `put`, `get` and `delete_job`, and
check it with the conformance kit (a missing method cannot be built; a broken rule raises `AssertionError`):

```python
from featherweight import AgentRunner, ResultKey, ResultStore, testing


class MyStore(ResultStore):
    async def put(self, key: ResultKey, output: dict) -> None: ...  # key: job_id, agent_hash, input_hash
    async def get(self, key: ResultKey) -> dict | None: ...
    async def delete_job(self, job_id: str) -> None: ...


await testing.check_result_store(lambda: MyStore())
runner = AgentRunner(db, results=MyStore())
```

The first job has to be created `resumable=True`. `examples/results/store.py` is a whole store (a JSON file per result).
