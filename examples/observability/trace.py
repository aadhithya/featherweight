"""Run a job and print its trace, with a span of your own inside it.

    python examples/observability/trace.py

No model and no key. Needs `opentelemetry-sdk` (in the dev group): a real app configures an exporter for its own backend.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from featherweight import AgentRunner, JobContext, OTelObserver, SqliteDatabase, job, run_job  # noqa: E402

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))  # to the console; yours goes to your backend
trace.set_tracer_provider(provider)


class Text(BaseModel):
    text: str


class Shouted(BaseModel):
    shouted: str


@job
async def shout(ctx: JobContext, input: Text) -> Shouted:
    with trace.get_tracer("my_app").start_as_current_span("make it loud"):  # your span, under the job's
        return Shouted(shouted=input.text.upper())


async def main() -> None:
    async with SqliteDatabase(":memory:") as db:  # no file
        runner = AgentRunner(db, observers=[OTelObserver()])
        outcome = await run_job(runner, shout, Text(text="hello"))
        print(outcome.result)


if __name__ == "__main__":
    asyncio.run(main())
