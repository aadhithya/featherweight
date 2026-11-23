"""An agent that launches other agents, to run: an editor that gets help from a summariser and a translator.

    python examples/launching/agent.py                 # two sample requests
    python examples/launching/agent.py "your request"  # your own request

Needs GOOGLE_API_KEY. The `Editor` may launch two agents, and only those: it is told their names and descriptions, asks
for the input one expects with `get_agent_schema`, and launches it with `launch_agent`. After each request the script
prints the runs of the job as a tree, which is how launches are recorded: every row knows the attempt that launched it.

Set FTW_LOG_LEVEL=INFO (`FTW_LOG_LEVEL=INFO make example-launching`) to see the log lines: each attempt, each launch and each token count.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from featherweight import (  # noqa: E402
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    Status,
    default_db_path,
    setup_logging,
)


class SummariserInput(AgentInput):
    text: str
    max_words: int


class SummariserOutput(AgentOutput):
    summary: str | None = None


class Summariser(AgentBase):
    name: str = "summariser"
    description: str = "Summarises a text in at most the given number of words"
    version: str = "0.1.0"
    prompt: str = "Summarise the text in at most `max_words` words. Keep the figures."
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    input_model: ClassVar[type[AgentInput]] = SummariserInput
    output_model: ClassVar[type[AgentOutput]] = SummariserOutput


class TranslatorInput(AgentInput):
    text: str
    language: str


class TranslatorOutput(AgentOutput):
    translation: str | None = None


class Translator(AgentBase):
    name: str = "translator"
    description: str = "Translates a text into the given language"
    version: str = "0.1.0"
    prompt: str = "Translate the text into the given language. Return only the translation."
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    input_model: ClassVar[type[AgentInput]] = TranslatorInput
    output_model: ClassVar[type[AgentOutput]] = TranslatorOutput


class EditorInput(AgentInput):
    request: str


class EditorOutput(AgentOutput):
    headline: str | None = None
    summary: str | None = None


class Editor(AgentBase):
    name: str = "editor"
    description: str = "Writes a headline and a short summary for an article, in the language asked for"
    version: str = "0.1.0"
    prompt: str = (
        "Do what the request asks. Write the headline yourself. Get the summary from the summariser, and if the "
        "request asks for another language, get the translator to translate the headline and the summary. Return "
        "the headline and the summary in `headline` and `summary`."
    )
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    subagents: list[type] = [Summariser, Translator]  # the only agents the editor can launch
    input_model: ClassVar[type[AgentInput]] = EditorInput
    output_model: ClassVar[type[AgentOutput]] = EditorOutput


ARTICLE = (
    "The harbour authority has approved a new cold-storage warehouse at Pier 4. Construction starts in March and "
    "should take fourteen months. The warehouse will hold 6,000 pallets of fresh produce and create about 120 jobs."
)
SAMPLES = [
    f"Write a headline and a summary of at most 20 words for this article. {ARTICLE}",
    f"Write a headline and a summary of at most 20 words for this article, in French. {ARTICLE}",
]


def print_tree(rows) -> None:
    """The runs of one job, each under the attempt that launched it."""
    children: dict[str | None, list] = {}
    for row in rows:
        children.setdefault(row.parent_attempt_id, []).append(row)

    def show(row, indent: int) -> None:
        tokens = f"{row.input_tokens or 0} in / {row.output_tokens or 0} out"
        print(f"  {'  ' * indent}{row.agent_name}  attempt {row.attempt}  {row.status.value}  {tokens}")
        for child in children.get(row.attempt_id, []):
            show(child, indent + 1)

    for root in children.get(None, []):
        show(root, 0)


async def main(requests: list[str]) -> int:
    if os.environ.get("FTW_LOG_LEVEL"):  # the log is for when you ask for it: the answers are what this prints
        setup_logging()
    failed = 0
    async with SqliteDatabase(default_db_path()) as db:
        runner = AgentRunner(db)
        for request in requests:
            print(f"\n> {request}")
            job_id = await db.create_job("launching-demo")
            result = await runner.run(Editor, EditorInput(request=request), job_id=job_id)
            if result.status == Status.SUCCESS and result.output is not None:
                print(f"\n{result.output.headline}\n{result.output.summary}\n\nruns of this job:")
            else:
                failed += 1
                print(f"\nfailed: {result.error}\n\nruns of this job:")
            print_tree(await db.list_attempts(job_id=job_id))
    return 1 if failed else 0


if __name__ == "__main__":
    # Words after the script are one request (`make` passes `ARGS="..."` as separate words); none means the samples.
    request = " ".join(sys.argv[1:]).strip()
    sys.exit(asyncio.run(main([request] if request else SAMPLES)))
