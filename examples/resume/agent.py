"""Resuming a job, to run: a pipeline that crashes after its first step, and a second run that does not repeat it.

    python examples/resume/agent.py

Needs ANTHROPIC_API_KEY and the `anthropic` extra. A release-notes job: a `Summariser` summarises each of four commit
messages, then a `ReleaseNotes` agent writes the notes from the summaries. The first run is made to crash in the
job's own code after the summaries, as if the process had died. The second run uses `resume_from` the first: the
four summaries are answered from the results the first job kept (no model call, no tokens), and only the notes are written.
The results are kept by the **default result store**, a table in the run log's sqlite file: the example does not pass one.
To keep them somewhere else, write your own (see examples/results). After each run the script prints the runs of the job, so
you can see which were reused.

Set FTW_LOG_LEVEL=INFO (`FTW_LOG_LEVEL=INFO make example-resume`) to see the log lines, including `run_reused`.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from featherweight import (  # noqa: E402
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Claude,
    JobContext,
    ModelSettings,
    RunStatus,
    SqliteDatabase,
    default_db_path,
    job,
    run_job,
    setup_logging,
)

# The smallest Claude model, with thinking off and a small output limit: a run costs next to nothing.
HAIKU = Claude(model="claude-haiku-5-5", effort="low", thinking="off", max_output_tokens=1000)


class SummaryInput(AgentInput):
    commit: str


class SummaryOutput(AgentOutput):
    summary: str | None = None


class Summariser(AgentBase):
    name: str = "summariser"
    description: str = "Summarises a commit message in one short line for release notes"
    version: str = "0.1.0"
    prompt: str = "Summarise the commit message in one line of at most 12 words, for release notes. Keep the figures."
    model: ModelSettings = HAIKU
    input_model: ClassVar[type[AgentInput]] = SummaryInput
    output_model: ClassVar[type[AgentOutput]] = SummaryOutput


class NotesInput(AgentInput):
    summaries: list[str]


class NotesOutput(AgentOutput):
    notes: str | None = None


class ReleaseNotes(AgentBase):
    name: str = "release_notes"
    description: str = "Writes release notes from a list of one-line summaries"
    version: str = "0.1.0"
    prompt: str = (
        "Write short release notes from the summaries: a one-sentence overview, then a bullet for each change."
    )
    model: ModelSettings = HAIKU
    input_model: ClassVar[type[AgentInput]] = NotesInput
    output_model: ClassVar[type[AgentOutput]] = NotesOutput


COMMITS = [
    "Fix the crash when a job id is unknown: look it up first and raise a clear error. Closes #41.",
    "Add a retry delay setting, 1 second by default, so a retry does not hit a busy server at once.",
    "Speed up the run log: add an index, and list runs 40% faster on a database with 100,000 rows.",
    "Drop support for Python 3.11. The minimum is now Python 3.12.",
]


class Commits(BaseModel):
    commits: list[str]
    crash: bool = False  # to show a failure: the job dies after the summaries


class Notes(BaseModel):
    notes: str


@job
async def release_notes(ctx: JobContext, input: Commits) -> Notes:
    """The job is plain async Python. Everything between the agent calls is deterministic, which is what lets a resumed
    run ask for the same things again, and find them in the log."""
    results = await ctx.run_many(Summariser, [SummaryInput(commit=c) for c in input.commits])
    summaries = [r.output.summary for r in results if r.status == RunStatus.SUCCESS and r.output and r.output.summary]
    if input.crash:
        raise RuntimeError("simulated crash: the process died after the summaries")
    notes = await ctx.run(ReleaseNotes, NotesInput(summaries=summaries))
    if notes.status != RunStatus.SUCCESS or not notes.output or not notes.output.notes:
        raise RuntimeError("the notes were not written")
    return Notes(notes=notes.output.notes)


def print_runs(rows) -> None:
    for row in rows:
        tokens = f"{row.input_tokens or 0} in / {row.output_tokens or 0} out"
        reused = "   reused, no model call" if row.reused_from_agent_run_id else ""
        print(f"  {row.agent_name:<14} {row.status.value:<8} {tokens}{reused}")


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("This example uses Claude: set ANTHROPIC_API_KEY in .env (see .env.example).")
        return 2
    if os.environ.get("FTW_LOG_LEVEL"):  # the log is for when you ask for it: the answers are what this prints
        setup_logging()
    else:  # the first run fails on purpose: leave its error and traceback out of the output
        logging.getLogger("featherweight").setLevel(logging.CRITICAL)
    async with SqliteDatabase(default_db_path()) as db:
        runner = AgentRunner(db)

        print("first run: the process dies after the summaries")
        first = await run_job(runner, release_notes, Commits(commits=COMMITS, crash=True), resumable=True)
        print(f"  job {first.status.value}: {first.error}")
        print_runs(await db.list_runs(job_id=first.job_id))

        print("\nsecond run: the same job, with resume_from the first one")
        second = await run_job(runner, release_notes, Commits(commits=COMMITS), resume_from=first.job_id)
        print(f"  job {second.status.value}")
        print_runs(await db.list_runs(job_id=second.job_id))
        if second.result:
            print(f"\n{second.result.notes}")
    return 0 if second.status == RunStatus.SUCCESS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
