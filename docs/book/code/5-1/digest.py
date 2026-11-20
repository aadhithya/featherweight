"""The job from Part 3: summarise the reports, the most severe first."""

from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from pydantic import BaseModel

from featherweight import JobContext, job

RANK = {"high": 0, "medium": 1, "low": 2, None: 3}  # most severe first; no severity last


class Reports(BaseModel):
    paths: list[str]


class Digest(BaseModel):
    summaries: list[Summary]


@job  # a class made from this function: a name, a version, a typed input and output
async def summarise_all(ctx: JobContext, input: Reports) -> Digest:
    reports = [Report(text=Path(path).read_text()) for path in input.paths]
    results = await ctx.run_many(IncidentSummariser, reports)
    summaries: list[Summary] = [r.output for r in results if r.output]
    summaries.sort(key=lambda s: RANK[s.severity])
    return Digest(summaries=summaries)
