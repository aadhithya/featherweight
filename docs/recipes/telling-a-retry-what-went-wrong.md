# Telling a retry what went wrong

A retry gets the same input as the try before it, so it cannot learn from the error. Put the error in the input, and the next try can fix what was wrong.

```python
import asyncio
from typing import ClassVar

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    Status,
    default_db_path,
)


class FillInput(AgentInput):
    form: str
    problems: list[str] = []  # what was wrong with the last try


class Filled(AgentOutput):
    name: str = ""
    year: int = 0


class Filler(AgentBase):
    name: str = "filler"
    description: str = "Fills in a form"
    version: str = "0.1.0"
    prompt: str = "Fill in the form. If the input lists problems, fix them."
    model: ModelSettings = Gemini()
    input_model: ClassVar[type[AgentInput]] = FillInput
    output_model: ClassVar[type[AgentOutput]] = Filled

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject
        if not 1000 <= output.year <= 2100:
            raise ValueError(f"The year {output.year} is not a four-digit year")


async def main() -> None:
    form = "Ada Lovelace, born in the eighteen-hundreds"
    async with (
        SqliteDatabase(default_db_path()) as db,
        AgentRunner(db, max_attempts=1) as runner,
    ):  # one try: the loop is the retry
        job_id = await db.create_job("fill-in")  # a job id groups the runs of one piece of work
        result = await runner.run(Filler, FillInput(form=form), job_id=job_id)
        for _ in range(2):
            if result.status == Status.SUCCESS:
                break
            result = await runner.run(Filler, FillInput(form=form, problems=[result.error or ""]), job_id=job_id)
        print(result.status.value, result.output or result.error)


if __name__ == "__main__":
    asyncio.run(main())
```
