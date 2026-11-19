"""A result store of your own, and how to check it. No model, no key.

    python examples/results/store.py

Resuming a job reads the outputs of finished steps from a `ResultStore`. Write three methods, and the conformance kit tells you
whether it behaves. This one keeps a JSON file per result, in a folder per job.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

import featherweight as ftw  # noqa: E402


class FileResultStore(ftw.ResultStore):
    def __init__(self, folder: Path):
        self.folder = folder

    def _file(self, key: ftw.ResultKey) -> Path:
        return self.folder / key.job_id / f"{key.agent_hash}-{key.input_hash}.json"

    async def put(self, key: ftw.ResultKey, output: dict) -> None:
        self._file(key).parent.mkdir(parents=True, exist_ok=True)
        self._file(key).write_text(json.dumps(output))

    async def get(self, key: ftw.ResultKey) -> dict | None:
        file = self._file(key)
        return json.loads(file.read_text()) if file.exists() else None

    async def delete_job(self, job_id: str) -> None:
        for file in (self.folder / job_id).glob("*.json"):
            file.unlink()


async def main() -> None:
    await ftw.testing.check_result_store(
        lambda: FileResultStore(Path(tempfile.mkdtemp()))
    )  # raises if a rule is broken
    print("FileResultStore follows the ResultStore contract")


if __name__ == "__main__":
    asyncio.run(main())
