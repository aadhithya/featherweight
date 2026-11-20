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
