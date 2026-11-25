"""Skills: instructions an agent can read when it needs them, as typed Pydantic models.

A skill follows the Agent Skills specification (https://agentskills.io/specification): a directory with a `SKILL.md`
(YAML frontmatter, then Markdown instructions) and, optionally, other files such as `references/`, `scripts/` and
`assets/`. Skills are loaded in steps, so that they cost little context until they are needed:

1. the `name` and `description` are in the agent's prompt (that is how the agent knows the skill exists),
2. the instructions are read with the `get_skill` tool when the agent decides the skill applies,
3. other files of the skill are read with `get_skill` too, only when the instructions point to them.

A `Skill` is plain data, so it is defined as an instance (or as a subclass with defaults) and listed in an agent's
`skills`; or it is loaded from a skill directory with `Skill.from_directory`. Scripts are kept as text for the agent
to read: nothing here runs them. `allowed_tools` is kept but not enforced. A skill whose text lives elsewhere is a subclass that
overrides `aread`.
"""

import logging
import re
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

MAX_RESOURCE_CHARS = 256  # the longest path that a model may ask a skill for

SKILL_FILE = "SKILL.md"
# 1 to 64 characters, lowercase letters, digits and single hyphens, none at the start or the end.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_COMPATIBILITY_CHARS = 500
RECOMMENDED_MAX_LINES = 500  # the spec recommends instructions under 500 lines; a longer one is only logged


class SkillError(ValueError):
    """A skill directory or SKILL.md that does not follow the specification."""


class Skill(BaseModel):
    # Unknown frontmatter keys are ignored: skills written for other tools carry extra ones. Everything the
    # specification defines is checked.
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    name: str = Field(
        description="1-64 characters: lowercase letters, digits and single hyphens. Matches the directory."
    )
    description: str = Field(description="What the skill does and when to use it, in 1-1024 characters.")
    instructions: str = Field(default="", description="The Markdown body of SKILL.md.")
    license: str | None = None
    compatibility: str | None = Field(default=None, description="Environment requirements, at most 500 characters.")
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: str | None = Field(default=None, alias="allowed-tools", description="Space-separated; not enforced.")
    resources: dict[str, str] = Field(
        default_factory=dict, description="Other files of the skill: path relative to the skill root, and its text."
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if len(value) > MAX_NAME_CHARS or not NAME_PATTERN.match(value):
            raise ValueError(
                f"skill name {value!r} must be 1-{MAX_NAME_CHARS} characters of lowercase letters, digits and single "
                "hyphens, and not start or end with a hyphen"
            )
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        if not value.strip() or len(value) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"skill description must be 1-{MAX_DESCRIPTION_CHARS} characters, got {len(value)}")
        return value

    @field_validator("compatibility")
    @classmethod
    def _compatibility(cls, value: str | None) -> str | None:
        if value is not None and not (1 <= len(value) <= MAX_COMPATIBILITY_CHARS):
            raise ValueError(f"skill compatibility must be 1-{MAX_COMPATIBILITY_CHARS} characters, got {len(value)}")
        return value

    @field_validator("resources")
    @classmethod
    def _resources(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value:
            parts = PurePosixPath(path).parts
            if not parts or PurePosixPath(path).is_absolute() or ".." in parts or path == SKILL_FILE:
                raise ValueError(f"resource path {path!r} must be relative, inside the skill, and not SKILL.md")
        return value

    # reading the skill, for the `get_skill` tool

    def read(self, resource: str = "") -> str:
        """The instructions, with the other files of the skill listed; or the text of one other file."""
        if not resource:
            listing = "\n".join(f"- {path}" for path in sorted(self.resources))
            more = f"\n\nOther files of this skill (ask for one with `resource`):\n{listing}" if listing else ""
            return f"# Skill: {self.name}\n\n{self.instructions.strip()}{more}"
        text = self.resources.get(PurePosixPath(resource).as_posix())
        if text is None:
            listing = ", ".join(sorted(self.resources)) or "none"
            return f"The skill '{self.name}' has no file '{resource}'. Its files: {listing}."
        return text

    async def aread(self, resource: str = "") -> str:
        """`read`, asynchronously: the one the runner's `get_skill` tool awaits. The default returns `read`. A skill whose text is
        somewhere else (a database, an object store) is a subclass that overrides this and fetches without blocking the event loop.
        It should return text; if it raises, the agent is told the skill could not be read, and the error is logged."""
        return self.read(resource)

    # loading a skill that follows the specification

    @classmethod
    def from_markdown(cls, text: str, *, resources: dict[str, str] | None = None) -> "Skill":
        """Parse the text of a SKILL.md: YAML frontmatter between `---` lines, then the instructions."""
        match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", text, re.DOTALL)
        if match is None:
            raise SkillError("SKILL.md must start with YAML frontmatter between --- lines")
        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise SkillError(f"the frontmatter of SKILL.md is not valid YAML: {exc}") from exc
        if not isinstance(frontmatter, dict):
            raise SkillError("the frontmatter of SKILL.md must be a mapping")
        # YAML turns `version: 1.0` into a number; the specification wants text values in `metadata`.
        if isinstance(frontmatter.get("metadata"), dict):
            frontmatter["metadata"] = {str(k): str(v) for k, v in frontmatter["metadata"].items()}
        body = match.group(2).strip("\r\n")
        if body.count("\n") + 1 > RECOMMENDED_MAX_LINES:
            logger.warning(
                "skill_long name=%s lines=%d recommended=%d",
                frontmatter.get("name"),
                body.count("\n") + 1,
                RECOMMENDED_MAX_LINES,
            )
        return cls(**frontmatter, instructions=body, resources=resources or {})

    @classmethod
    def from_directory(cls, path: str | Path) -> "Skill":
        """Load a skill directory: SKILL.md, and every other text file below it as a resource.

        The name in SKILL.md must match the name of the directory, as the specification says. Files that are not
        UTF-8 text are left out (and logged), because the agent can only read text.
        """
        root = Path(path)
        skill_file = root / SKILL_FILE
        if not skill_file.is_file():
            raise SkillError(f"{root} has no {SKILL_FILE}")
        resources: dict[str, str] = {}
        for file in sorted(p for p in root.rglob("*") if p.is_file() and p != skill_file):
            relative = file.relative_to(root).as_posix()
            if any(part.startswith(".") for part in file.relative_to(root).parts):
                continue  # hidden files such as .DS_Store
            try:
                resources[relative] = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning("skill_file_skipped skill=%s file=%s reason=not_utf8_text", root.name, relative)
        skill = cls.from_markdown(skill_file.read_text(encoding="utf-8"), resources=resources)
        if skill.name != root.resolve().name:
            raise SkillError(f"the skill name {skill.name!r} must match its directory name {root.resolve().name!r}")
        return skill


def clean_resource(resource: str) -> str | None:
    """The path that a model asked for, as a path inside the skill, or `None` when it is not one (docs/specs/skills.md).

    The empty string is the skill itself. A `.` segment is dropped and `//` is one `/`. Refused: a path that starts with `/`, has a backslash,
    a `..` segment (it is not resolved), a control character (NUL and newline included), is longer than `MAX_RESOURCE_CHARS`, or is only dots
    and slashes. The model chose it, so this is the one place that decides, for every skill."""
    if resource == "":
        return ""
    if len(resource) > MAX_RESOURCE_CHARS or resource.startswith("/") or "\\" in resource:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in resource):
        return None
    parts = [part for part in resource.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)
