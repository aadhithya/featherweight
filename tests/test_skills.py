"""Skills: the model, loading from the specification's format, and an agent reading its skills."""

import asyncio
import logging
import time
from pathlib import Path
from typing import ClassVar

import pytest
from fakes import FakeSettings, Scripted, runner_with, settings, structured_reply, tool_reply
from langchain_core.messages import SystemMessage, ToolMessage
from pydantic import BaseModel, ValidationError

from featherweight.context import CURRENT_IDENTITY
from featherweight.fingerprint import agent_hash, tools_hash
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, Status
from featherweight.runner import AgentRunner
from featherweight.skills import Skill, SkillError

EXAMPLE = Path(__file__).parent.parent / "examples" / "skills" / "meeting-notes"
VALID = (
    "---\nname: pdf-processing\ndescription: Extracts text from PDFs. Use when a PDF is mentioned.\n---\n# PDFs\nDo it."
)


def skill(**kwargs) -> Skill:
    return Skill(**{"name": "pdf-processing", "description": "Extracts text from PDFs.", **kwargs})


# the model, as the specification defines it


@pytest.mark.parametrize("name", ["pdf-processing", "a", "pdf2", "a-b-c", "x" * 64])
def test_valid_names(name):
    assert skill(name=name).name == name


@pytest.mark.parametrize(
    "name",
    ["PDF-Processing", "-pdf", "pdf-", "pdf--processing", "pdf_processing", "pdf processing", "", "x" * 65],
    ids=[
        "uppercase",
        "leading hyphen",
        "trailing hyphen",
        "double hyphen",
        "underscore",
        "space",
        "empty",
        "65 characters",
    ],
)
def test_invalid_names(name):
    with pytest.raises(ValidationError, match="skill name"):
        skill(name=name)


@pytest.mark.parametrize("description", ["", "   ", "d" * 1025])
def test_invalid_descriptions(description):
    with pytest.raises(ValidationError, match="description"):
        skill(description=description)


def test_description_limits_are_inclusive():
    assert len(skill(description="d" * 1024).description) == 1024


@pytest.mark.parametrize(
    "compatibility,ok", [(None, True), ("Needs git", True), ("c" * 500, True), ("", False), ("c" * 501, False)]
)
def test_compatibility_is_1_to_500_characters_when_given(compatibility, ok):
    if ok:
        assert skill(compatibility=compatibility).compatibility == compatibility
    else:
        with pytest.raises(ValidationError, match="compatibility"):
            skill(compatibility=compatibility)


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.md", "a/../../b.md", "SKILL.md", ""])
def test_resources_must_stay_inside_the_skill(path):
    with pytest.raises(ValidationError, match="resource path"):
        skill(resources={path: "x"})


def test_a_skill_is_immutable_and_takes_allowed_tools_by_its_specification_name():
    s = Skill(**{"name": "x", "description": "d", "allowed-tools": "Bash(git:*) Read"})
    assert s.allowed_tools == "Bash(git:*) Read"
    with pytest.raises(ValidationError):
        s.name = "y"


# reading a skill


def test_reading_gives_the_instructions_and_lists_the_other_files():
    s = skill(instructions="Do the thing.", resources={"references/a.md": "A", "scripts/run.py": "print(1)"})
    text = s.read()
    assert text.startswith("# Skill: pdf-processing") and "Do the thing." in text
    assert (
        "- references/a.md" in text
        and "- scripts/run.py" in text
        and "A" not in text.split("Other files")[0].split("Do the thing.")[1]
    )


def test_reading_a_skill_without_other_files_lists_none():
    assert "Other files" not in skill(instructions="Do it.").read()


def test_reading_a_resource_gives_its_text_and_a_missing_one_lists_the_files():
    s = skill(resources={"references/a.md": "the A file"})
    assert s.read("references/a.md") == "the A file"
    missing = s.read("references/zzz.md")
    assert "no file 'references/zzz.md'" in missing and "references/a.md" in missing


# loading, from the format of the specification


def test_from_markdown_reads_the_frontmatter_and_the_body():
    s = Skill.from_markdown(VALID)
    assert (s.name, s.description) == ("pdf-processing", "Extracts text from PDFs. Use when a PDF is mentioned.")
    assert s.instructions == "# PDFs\nDo it."


def test_from_markdown_reads_every_optional_field_and_ignores_unknown_ones():
    s = Skill.from_markdown(
        "---\nname: x\ndescription: d\nlicense: MIT\ncompatibility: Needs git\nallowed-tools: Read Grep\n"
        "metadata:\n  author: me\n  version: 1.0\nmodel: some-other-tools-field\n---\nBody"
    )
    assert (s.license, s.compatibility, s.allowed_tools) == ("MIT", "Needs git", "Read Grep")
    assert s.metadata == {"author": "me", "version": "1.0"}  # YAML read 1.0 as a number; the specification wants text


@pytest.mark.parametrize(
    "text,message",
    [
        ("# no frontmatter", "must start with YAML frontmatter"),
        ("---\nname: x\ndescription: d\nBody without a closing line", "must start with YAML frontmatter"),
        ("---\nname: [unclosed\n---\nBody", "not valid YAML"),
        ("---\n- just\n- a list\n---\nBody", "must be a mapping"),
    ],
    ids=["none", "unclosed", "bad yaml", "not a mapping"],
)
def test_from_markdown_refuses_what_is_not_a_skill_file(text, message):
    with pytest.raises(SkillError, match=message):
        Skill.from_markdown(text)


def test_from_markdown_requires_the_name_and_the_description():
    with pytest.raises(ValidationError, match="description"):
        Skill.from_markdown("---\nname: x\n---\nBody")
    with pytest.raises(ValidationError, match="name"):
        Skill.from_markdown("---\ndescription: d\n---\nBody")


def test_the_example_skill_loads_with_its_reference_file():
    s = Skill.from_directory(EXAMPLE)
    assert s.name == "meeting-notes" and s.metadata == {"author": "featherweight", "version": "1.0"}
    assert list(s.resources) == ["references/tags.md"] and "[DECISION]" in s.resources["references/tags.md"]
    assert s.instructions.startswith("# Meeting notes")


EXAMPLE_SKILLS = sorted(path for path in EXAMPLE.parent.iterdir() if (path / "SKILL.md").is_file())


@pytest.mark.parametrize("path", EXAMPLE_SKILLS, ids=lambda p: p.name)
def test_every_example_skill_follows_the_specification(path):
    s = Skill.from_directory(path)  # the name must match the directory, and every limit is checked
    assert s.instructions and len(s.instructions.splitlines()) < 500
    for name in s.resources:  # the instructions only point to files that exist
        assert name in s.instructions or not name.startswith("references/"), (
            f"{name} is not mentioned in the instructions"
        )


def make_dir(tmp_path, name="pdf-processing", text=VALID):
    root = tmp_path / name
    root.mkdir()
    (root / "SKILL.md").write_text(text, encoding="utf-8")
    return root


def test_from_directory_loads_nested_text_files_and_skips_hidden_and_binary_ones(tmp_path, caplog):
    root = make_dir(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (root / ".DS_Store").write_text("junk")
    (root / "logo.png").write_bytes(b"\x89PNG\xff\xfe\x00")
    with caplog.at_level(logging.WARNING, logger="featherweight.skills"):
        s = Skill.from_directory(root)
    assert s.resources == {"scripts/run.py": "print(1)"}
    assert "skill_file_skipped skill=pdf-processing file=logo.png" in caplog.text


def test_from_directory_requires_the_name_to_match_the_directory(tmp_path):
    with pytest.raises(SkillError, match="must match its directory name"):
        Skill.from_directory(make_dir(tmp_path, name="something-else"))


def test_from_directory_requires_a_skill_file(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SkillError, match="no SKILL.md"):
        Skill.from_directory(tmp_path / "empty")


# an agent with skills


class TaskInput(AgentInput):
    text: str


class TaskOutput(AgentOutput):
    notes: str | None = None


class Writer(AgentBase):
    name: str = "writer"
    description: str = "Writes notes"
    version: str = "0.1.0"
    prompt: str = "Write the notes."
    model: ModelSettings = FakeSettings()
    input_model: ClassVar[type[AgentInput]] = TaskInput
    output_model: ClassVar[type[AgentOutput]] = TaskOutput


NOTES = Skill.from_directory(EXAMPLE)
OTHER = skill(instructions="Other instructions.")


class SkilledWriter(Writer):
    skills: list[Skill] = [NOTES, OTHER]


def done():
    return structured_reply("TaskOutput", {"notes": "ok"})


def system_prompts(model) -> set[str]:
    return {m.content for messages in model.seen for m in messages if isinstance(m, SystemMessage)}


def tool_texts(model) -> list[str]:
    return [m.content for m in model.seen[-1] if isinstance(m, ToolMessage)]


async def test_the_prompt_lists_each_skill_by_name_and_description_but_not_its_instructions(db):
    model = Scripted(script=[done()])

    result = await runner_with(db, model).run(SkilledWriter, TaskInput(text="t"))

    assert result.status == Status.SUCCESS
    (prompt,) = system_prompts(model)
    assert f"- meeting-notes: {NOTES.description}" in prompt and f"- pdf-processing: {OTHER.description}" in prompt
    assert "get_skill" in prompt
    assert "Keep every note" not in prompt and "Leave out small talk" not in prompt  # the instructions are not in it
    assert (await db.list_attempts())[0].tools_hash == tools_hash([], ["get_skill"])


async def test_an_agent_without_skills_has_neither_the_prompt_section_nor_the_tool(db):
    model = Scripted(script=[done()])

    await runner_with(db, model).run(Writer, TaskInput(text="t"))

    (prompt,) = system_prompts(model)
    assert "skills" not in prompt.lower() and "get_skill" not in prompt
    assert (await db.list_attempts())[0].tools_hash == tools_hash([], [])


async def test_get_skill_returns_the_instructions_then_a_file_on_request(db):
    model = Scripted(
        script=[
            tool_reply("get_skill", {"name": "meeting-notes"}, "t1"),
            tool_reply("get_skill", {"name": "meeting-notes", "resource": "references/tags.md"}, "t2"),
            done(),
        ]
    )

    result = await runner_with(db, model).run(SkilledWriter, TaskInput(text="t"))

    assert result.status == Status.SUCCESS
    instructions, tags = tool_texts(model)
    assert instructions.startswith("# Skill: meeting-notes") and "references/tags.md" in instructions
    assert tags == NOTES.resources["references/tags.md"]


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"name": "stranger"}, "not one of your skills"),
        ({"name": "meeting-notes", "resource": "references/missing.md"}, "no file 'references/missing.md'"),
        ({"name": "meeting-notes", "resource": "../../etc/passwd"}, "no file"),
    ],
    ids=["unknown skill", "unknown file", "path outside the skill"],
)
async def test_get_skill_tells_the_agent_what_is_wrong_and_the_run_carries_on(db, args, expected):
    model = Scripted(script=[tool_reply("get_skill", args, "t"), done()])

    result = await runner_with(db, model).run(SkilledWriter, TaskInput(text="t"))

    assert result.status == Status.SUCCESS
    (text,) = tool_texts(model)
    assert expected in text


async def test_an_agent_can_have_skills_and_subagents_and_gets_both_sets_of_tools(db):
    class Both(SkilledWriter):
        subagents: list[type] = [Writer]

    model = Scripted(script=[done()])

    await runner_with(db, model).run(Both, TaskInput(text="t"))

    (prompt,) = system_prompts(model)
    assert "- writer: Writes notes" in prompt and "- meeting-notes:" in prompt
    assert (await db.list_attempts())[0].tools_hash == tools_hash([], ["launch_agent", "get_agent_schema", "get_skill"])


def test_skills_with_the_same_name_are_refused():
    with pytest.raises(ValueError, match="duplicate names"):
        Writer(skills=[skill(), skill(description="Another")])


def test_the_prompt_hash_changes_with_the_skills():
    assert Writer().system_prompt_hash != SkilledWriter().system_prompt_hash
    assert SkilledWriter().system_prompt_hash != Writer(skills=[NOTES]).system_prompt_hash


# a skill whose text is somewhere else

BUCKET: dict[str, str] = {}


class BucketSkill(Skill):
    """A skill that reads its text from a fake object store, when it is asked for."""

    etag: str = ""

    async def aread(self, resource: str = "") -> str:
        await asyncio.sleep(0)  # a real client would await the network here
        return BUCKET[f"{self.name}/{resource or 'SKILL.md'}"]


class BrokenSkill(Skill):
    async def aread(self, resource: str = "") -> str:
        raise ConnectionError("s3://private-bucket/key is not reachable")


def bucket_skill(etag: str = "1") -> BucketSkill:
    return BucketSkill(name="from-the-bucket", description="Kept in the bucket.", etag=etag)


async def test_aread_is_read_by_default():
    assert await NOTES.aread() == NOTES.read()
    assert await NOTES.aread("references/tags.md") == NOTES.read("references/tags.md")
    assert await NOTES.aread("references/missing.md") == NOTES.read("references/missing.md")


async def test_get_skill_awaits_aread_so_a_skill_can_read_its_text_from_somewhere_else(db):
    BUCKET.clear()
    BUCKET["from-the-bucket/SKILL.md"] = "Instructions from the bucket."
    BUCKET["from-the-bucket/references/a.md"] = "A file from the bucket."

    class FromTheBucket(Writer):
        skills: list[Skill] = [bucket_skill()]

    model = Scripted(
        script=[
            tool_reply("get_skill", {"name": "from-the-bucket"}, "t1"),
            tool_reply("get_skill", {"name": "from-the-bucket", "resource": "references/a.md"}, "t2"),
            done(),
        ]
    )

    result = await runner_with(db, model).run(FromTheBucket, TaskInput(text="t"))

    assert result.status == Status.SUCCESS
    assert tool_texts(model) == ["Instructions from the bucket.", "A file from the bucket."]
    assert "Instructions from the bucket." not in "".join(system_prompts(model))  # nothing was fetched for the prompt


def test_the_hash_of_an_agent_changes_with_a_field_of_its_fetched_skill():
    class First(Writer):
        skills: list[Skill] = [bucket_skill("1")]

    class Second(Writer):
        skills: list[Skill] = [bucket_skill("2")]

    assert agent_hash(First()) == agent_hash(First())
    assert agent_hash(First()) != agent_hash(Second())  # the etag is how an edit in the store reaches the hash


async def test_a_read_that_fails_is_text_for_the_agent_and_the_error_goes_to_the_log(db, caplog):
    class Broken(Writer):
        skills: list[Skill] = [BrokenSkill(name="broken", description="Never reachable.")]

    model = Scripted(script=[tool_reply("get_skill", {"name": "broken"}, "t"), done()])

    with caplog.at_level(logging.WARNING, logger="featherweight.runner"):
        result = await runner_with(db, model).run(Broken, TaskInput(text="t"))

    assert result.status == Status.SUCCESS  # the run carries on
    (text,) = tool_texts(model)
    assert text == "The skill 'broken' could not be read: ConnectionError."
    assert (
        "private-bucket" not in text
    )  # the message of the error can name a bucket or a key: it is not shown to the model
    assert any("skill_read_failed" in r.message and "private-bucket" in r.message for r in caplog.records)


# what the runner does around `aread`


class Recording(Skill):
    """A fetching skill that keeps what it was asked for, and answers with what it is told to."""

    asked: ClassVar[list[str]] = []
    answer: ClassVar[object] = "text"
    wait: ClassVar[float] = 0.0
    seen_identity: ClassVar[list[object]] = []

    async def aread(self, resource: str = "") -> str:
        Recording.asked.append(resource)
        Recording.seen_identity.append(CURRENT_IDENTITY.get())
        await asyncio.sleep(Recording.wait)
        return Recording.answer


@pytest.fixture(autouse=True)
def fresh_recording():
    Recording.asked, Recording.seen_identity = [], []
    Recording.answer, Recording.wait = "text", 0.0


class Fetching(Writer):
    skills: list[Skill] = [Recording(name="fetched", description="Kept elsewhere.")]


async def ask_for(db, *resources: str, runner=None):
    calls = [tool_reply("get_skill", {"name": "fetched", "resource": r}, f"t{n}") for n, r in enumerate(resources)]
    model = Scripted(script=[*calls, done()])
    result = await (runner or runner_with(db, model)).run(Fetching, TaskInput(text="t"))
    assert result.status == Status.SUCCESS
    return tool_texts(model)


@pytest.mark.parametrize(
    "resource",
    [
        "../tenant-b/SKILL.md",
        "a/../../b",
        "a/../b",  # it stays inside the skill, and is still refused: `..` is never resolved
        "..",
        "/etc/passwd",
        "//host/share",
        "a\\b",
        "..\\x",
        "a\x00b",
        "a\nb",
        "x" * 257,
        ".",
        "./",
        "/",
    ],
)
async def test_a_path_that_is_not_inside_the_skill_is_refused_as_text_and_the_skill_is_not_asked(db, caplog, resource):
    with caplog.at_level(logging.WARNING):
        (text,) = await ask_for(db, resource)

    assert Recording.asked == []  # `aread` was not called
    assert text.startswith("The skill 'fetched' has no file '") and text.endswith("'.")
    assert len(text) < 140  # the path is cut: a model could send a very long one
    assert "skill_read_failed" not in caplog.text  # a refusal is not an error


@pytest.mark.parametrize(
    "resource,reaches",
    [
        ("", ""),
        ("references/a.md", "references/a.md"),
        ("a/b.md", "a/b.md"),
        ("./references/a.md", "references/a.md"),
        ("references//a.md", "references/a.md"),
        ("a/./b.md", "a/b.md"),
        ("x" * 256, "x" * 256),
        ("..a/b..", "..a/b.."),  # dots in a name are not a `..` segment
    ],
)
async def test_a_path_inside_the_skill_reaches_it_normalised(db, resource, reaches):
    (text,) = await ask_for(db, resource)

    assert Recording.asked == [reaches] and text == "text"


async def test_the_default_read_is_what_it_was_for_a_path_in_its_files_and_one_that_is_not(db):
    class Plain(Writer):
        skills: list[Skill] = [NOTES]

    model = Scripted(
        script=[
            tool_reply("get_skill", {"name": "meeting-notes", "resource": "./references/tags.md"}, "t1"),
            tool_reply("get_skill", {"name": "meeting-notes", "resource": "../x"}, "t2"),
            done(),
        ]
    )
    await runner_with(db, model).run(Plain, TaskInput(text="t"))

    assert tool_texts(model)[0] == NOTES.read("references/tags.md")
    assert tool_texts(model)[1] == "The skill 'meeting-notes' has no file '../x'."


@pytest.mark.parametrize("answer", [None, b"bytes", 7, ["a"]], ids=["None", "bytes", "an int", "a list"])
async def test_a_read_that_does_not_return_text_is_a_read_that_failed(db, caplog, answer):
    Recording.answer = answer
    with caplog.at_level(logging.WARNING, logger="featherweight.runner"):
        (text,) = await ask_for(db, "")

    assert text == "The skill 'fetched' could not be read: TypeError."  # not 'null', and not the repr of the value
    assert any("skill_read_failed" in r.message and "TypeError" in r.message for r in caplog.records)


async def test_an_empty_text_is_text(db):
    Recording.answer = ""
    assert await ask_for(db, "") == [""]


async def test_a_read_that_takes_longer_than_the_timeout_is_a_read_that_failed_and_the_run_goes_on(db, caplog):
    Recording.wait = 30
    model = Scripted(script=[tool_reply("get_skill", {"name": "fetched"}, "t"), done()])
    with settings(skill_read_timeout=0.1):  # read when the runner is built
        runner = runner_with(db, model)

    with caplog.at_level(logging.WARNING, logger="featherweight.runner"):
        started = time.perf_counter()
        result = await runner.run(Fetching, TaskInput(text="t"))

    assert result.status == Status.SUCCESS and time.perf_counter() - started < 5
    assert tool_texts(model) == ["The skill 'fetched' could not be read: TimeoutError."]
    assert any("skill_read_failed" in r.message and "TimeoutError" in r.message for r in caplog.records)


def test_the_timeout_is_15_seconds_unless_the_variable_says_otherwise_and_a_bad_one_is_refused(db, monkeypatch):
    assert AgentRunner(db).skill_read_timeout == 15.0
    monkeypatch.setenv("FTW_SKILL_READ_TIMEOUT", "2.5")
    assert AgentRunner(db).skill_read_timeout == 2.5
    for bad in ("0", "-1", "soon"):  # there is no way to switch it off
        monkeypatch.setenv("FTW_SKILL_READ_TIMEOUT", bad)
        with pytest.raises(ValueError, match="FTW_SKILL_READ_TIMEOUT"):
            AgentRunner(db)


class Teacher(BaseModel):
    user_id: str


async def test_a_fetching_skill_sees_who_the_run_is_for(db):
    model = Scripted(script=[tool_reply("get_skill", {"name": "fetched"}, "t"), done()])
    runner = runner_with(db, model, identity_type=Teacher)

    await runner.run(Fetching, TaskInput(text="t"), identity=Teacher(user_id="u-1"))

    assert Recording.seen_identity == [Teacher(user_id="u-1")]
