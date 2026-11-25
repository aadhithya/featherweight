"""The pipeline: summarise every report, the most severe first (Part 3). A report that fails fails the job."""

from pathlib import Path

from incident import IncidentSummariser, Report, Summary
from pydantic import BaseModel

from featherweight import JobContext, job

RANK = {"high": 0, "medium": 1, "low": 2, None: 3}  # the order is plain Python, not something to ask a model


class Reports(BaseModel):
    paths: list[str]


class Digest(BaseModel):
    summaries: list[Summary]


@job  # a class made from this function: a name, a version, a typed input and output
async def summarise_all(ctx: JobContext, input: Reports) -> Digest:
    reports = [Report(text=Path(path).read_text()) for path in input.paths]
    results = await ctx.run_many(IncidentSummariser, reports)  # one run for each report, at the same time
    failed = [path for path, result in zip(input.paths, results, strict=True) if result.output is None]
    if failed:  # a digest with gaps would look complete: fail, and say which reports
        raise RuntimeError(f"Could not summarise: {', '.join(failed)}")
    summaries: list[Summary] = [r.output for r in results if r.output]
    summaries.sort(key=lambda s: RANK[s.severity])  # plain Python, the same each time
    return Digest(summaries=summaries)
