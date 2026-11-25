# A skill for each department

The agent is written once, and each department's skills are data: build the agent class for the request, with that department's skills.

```python
# yours: rows_of (reads the skills of a department from your database) and runner (an AgentRunner)
from typing import ClassVar

from pydantic import Field, create_model

from featherweight import AgentBase, AgentInput, AgentOutput, Skill


class NotesInput(AgentInput):
    transcript: str


class NotesOutput(AgentOutput):
    notes: str = ""


class LectureNotes(AgentBase):
    name: str = "lecture_notes"
    description: str = "Turns a lecture transcript into notes"
    version: str = "1"
    prompt: str = "Write lecture notes from the transcript, in the style of the skill you have."
    input_model: ClassVar[type[AgentInput]] = NotesInput
    output_model: ClassVar[type[AgentOutput]] = NotesOutput


def for_department(skills: list[Skill]) -> type[LectureNotes]:
    """The same agent, with this department's skills."""
    return create_model(
        "DepartmentNotes", __base__=LectureNotes, skills=(list[Skill], Field(default_factory=lambda: skills))
    )


law = for_department([Skill.from_markdown(row["skill_md"]) for row in rows_of("law")])  # your database
result = await runner.run(law, NotesInput(transcript="..."))
```

<!-- skill:omit -->
- **The agent hash covers the skills,** so two departments are two agents (two hashes), the same department is the same one, and an edited skill invalidates a resumed job's reused steps.
- A skill can only **instruct** the agent: its tools stay what the developer gave it. Users can write skills safely in that sense; whether to allow it is your rule.
- Build the class when the request arrives: a class is cheap, and nothing is registered. See [agents](../agents.md#defining-an-agent): the runner sees a class either way.
<!-- /skill:omit -->
