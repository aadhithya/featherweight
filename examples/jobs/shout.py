"""The job all the examples here use: it shouts. Plain code, no model, so no key is needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from pydantic import BaseModel  # noqa: E402

from featherweight import JobContext, job  # noqa: E402


class Text(BaseModel):
    text: str


class Shouted(BaseModel):
    shouted: str


@job
async def shout(ctx: JobContext, input: Text) -> Shouted:
    return Shouted(shouted=input.text.upper())
