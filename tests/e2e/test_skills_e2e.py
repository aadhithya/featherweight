"""End-to-end test of an agent with a skill, with the real model: it has to read the skill, and then the file the skill
points to, because the tags it must use are only in that file. About four model calls. Run with
`make e2e ARGS="-k skills"` (needs GOOGLE_API_KEY in .env). Not part of `make test`.
"""

import re
from pathlib import Path
from typing import ClassVar

from micro.model_settings import Gemini, ModelSettings
from micro.models import AgentBase, AgentInput, AgentOutput, RunStatus
from micro.runner import AgentRunner
from micro.skills import Skill

SKILL = Skill.from_directory(Path(__file__).parent.parent.parent / "examples" / "skills" / "meeting-notes")
TAGS = {"[DECISION]", "[ACTION]", "[FYI]"}  # only in references/tags.md, not in SKILL.md

MEETING = (
    "Priya opened by saying the Q3 budget review is on track. The team agreed to move the launch from May 5 to May 19 "
    "because the payment provider is late. Tom will update the project plan by Friday. Lena mentioned that the office "
    "will be closed on Monday. Everyone talked a while about the new coffee machine."
)


class NotesInput(AgentInput):
    text: str


class NotesOutput(AgentOutput):
    notes: list[str] = []


class NoteWriter(AgentBase):
    name: str = "note_writer"
    description: str = "Writes notes for a meeting"
    version: str = "0.1.0"
    prompt: str = "Write the notes for the meeting in the text."
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    skills: list[Skill] = [SKILL]
    input_model: ClassVar[type[AgentInput]] = NotesInput
    output_model: ClassVar[type[AgentOutput]] = NotesOutput


async def test_an_agent_reads_its_skill_and_the_file_it_points_to(db):
    result = await AgentRunner(db).run(NoteWriter, NotesInput(text=MEETING))

    assert result.status == RunStatus.SUCCESS, result.error
    notes = result.output.notes
    print("\n[notes]", *notes, sep="\n  ")
    print(f"[tokens] in={result.input_tokens} out={result.output_tokens}")
    assert 1 <= len(notes) <= 5
    tags = {re.match(r"\s*(\[[A-Z]+\])", n).group(1) for n in notes if re.match(r"\s*(\[[A-Z]+\])", n)}
    assert len(tags) and tags <= TAGS, f"notes do not follow the skill: {notes}"
    assert "[DECISION]" in tags and "[ACTION]" in tags
    assert not any("coffee" in n.lower() for n in notes), "small talk was left in"
