"""An agent with skills, to run: it reads a skill only when a request needs it.

    python examples/skills/agent.py                 # three sample requests, one for each skill
    python examples/skills/agent.py "your request"  # your own request

Needs GOOGLE_API_KEY. The agent is told the name and description of its three skills (the folders next to this file); when a
request fits one, it reads that skill with `get_skill`, and then the files the skill points to, and follows them.
The script prints each skill the agent read.

Set FTW_LOG_LEVEL=INFO (`FTW_LOG_LEVEL=INFO make example-skills`) to see the log lines: each attempt, each skill read and each token count.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent / "src"))  # so that this runs without installing anything

import featherweight as ftw  # noqa: E402

SKILLS = [ftw.Skill.from_directory(path) for path in sorted(HERE.iterdir()) if (path / "SKILL.md").is_file()]


class AssistantInput(ftw.AgentInput):
    request: str


class AssistantOutput(ftw.AgentOutput):
    answer: str | None = None


class Assistant(ftw.AgentBase):
    name: str = "assistant"
    description: str = "Helps with short writing tasks"
    version: str = "0.1.0"
    prompt: str = (
        "You help with short writing tasks. If one of your skills fits the request, use it. "
        "Return the finished text, and nothing else, in `answer`."
    )
    model: ftw.ModelSettings = ftw.Gemini(model="gemini-3.5-flash-lite", thinking="low")
    skills: list[ftw.Skill] = SKILLS
    input_model: ClassVar[type[ftw.AgentInput]] = AssistantInput
    output_model: ClassVar[type[ftw.AgentOutput]] = AssistantOutput


SAMPLES = [
    "Write notes for this meeting: Priya said the Q3 budget review is on track. We agreed to move the launch from May 5 "
    "to May 19 because the payment provider is late. Tom will update the project plan by Friday. We spent a while on "
    "the new coffee machine.",
    "Reply to this email from Ms Okonkwo, a client: 'Could we move Thursday's demo to Friday? Some of my team cannot "
    "make Thursday.' Say yes: Friday at 10:00 works, and the video link stays the same.",
    "Write the release notes for version 2.4 from these changes: added dark mode; fixed the crash when exporting an "
    "empty report; the search is now twice as fast; refactored the settings module (no visible change); fixed the "
    "wrong total on invoices with discounts.",
]


class ShowSkillReads(logging.Handler):
    """Print each `skill_read` line the runner logs, so that you can see what the agent read."""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("skill_read"):
            fields = dict(part.split("=", 1) for part in message.split()[1:])
            what = fields["resource"] if fields["resource"] != "-" else "instructions"
            print(f"  [read] {fields['skill']}: {what}")


async def main(requests: list[str]) -> int:
    if os.environ.get("FTW_LOG_LEVEL"):  # the log is for when you ask for it: the answers are what this prints
        ftw.setup_logging()
    runner_log = logging.getLogger("featherweight.runner")
    runner_log.setLevel(logging.INFO)
    runner_log.addHandler(ShowSkillReads())
    failed = 0
    async with ftw.Database(ftw.default_db_path()) as db:
        runner = ftw.AgentRunner(db)
        for request in requests:
            print(f"\n> {request}")
            result = await runner.run(
                Assistant, AssistantInput(request=request), job_id=await db.create_job("skills-demo")
            )
            if result.status == ftw.RunStatus.SUCCESS and result.output is not None:
                print(f"\n{result.output.answer}\n  ({result.input_tokens} tokens in, {result.output_tokens} out)")
            else:
                failed += 1
                print(f"\nfailed: {result.error}")
    return 1 if failed else 0


if __name__ == "__main__":
    # Words after the script are one request (`make` passes `ARGS="..."` as separate words); none means the samples.
    request = " ".join(sys.argv[1:]).strip()
    sys.exit(asyncio.run(main([request] if request else SAMPLES)))
