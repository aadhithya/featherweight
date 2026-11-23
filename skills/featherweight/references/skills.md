<!-- Copied from docs/skills.md by scripts/build_skill.py. Edit the original, not this file. -->

# Skills

[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)

A skill is a set of instructions that an agent reads when it needs them. featherweight follows the
[Agent Skills specification](https://agentskills.io/specification): a skill is a directory with a `SKILL.md` and,
optionally, other files. Defining agents is in [agents.md](agents.md).

- [Using a skill](#using-a-skill)
- [How an agent uses its skills](#how-an-agent-uses-its-skills)
- [The skill model](#the-skill-model)
- [Loading a skill](#loading-a-skill)
- [What is supported](#what-is-supported)
- [Writing a good skill](#writing-a-good-skill)

## Using a skill

```python
from featherweight import Skill


class Writer(AgentBase):
    ...
    skills: list[Skill] = [Skill.from_directory("examples/skills/meeting-notes")]
```

A skill can also be written in code:

```python
Skill(
    name="release-notes",
    description="Turns changes into release notes. Use when asked for a changelog.",
    instructions="1. Start with the version.\n2. ...",
)
```

[`examples/skills`](https://github.com/aadhithya/featherweight/blob/main/examples/skills) is an agent with three skills to run (`make example-skills`): the agent is
`agent.py`, and each other folder is a skill.

## How an agent uses its skills

Skills are loaded in steps, so that an unused skill costs almost nothing. This is the specification's "progressive
disclosure".

| Step | What the agent has | How |
|---|---|---|
| 1. Known | The `name` and `description` of every skill | In the system prompt, always |
| 2. Activated | The instructions (the body of `SKILL.md`) | The agent calls `get_skill(name)` when a skill fits the task |
| 3. Used | Other files of the skill | The agent calls `get_skill(name, resource=path)` when the instructions point to a file |

**The system prompt** lists the skills and tells the agent to read the one that applies before it starts:

```
You have these skills, which are instructions for particular kinds of work:
- meeting-notes: Turns raw meeting text into short tagged notes. Use when asked to ...
When a skill applies to your task, call `get_skill` with its name before you start, and follow what it says. ...
```

**The tool**, `get_skill(name, resource="")`, is built by the runner for each attempt:

| Call | Returns |
|---|---|
| `get_skill("meeting-notes")` | `# Skill: meeting-notes`, the instructions, and a list of the skill's other files |
| `get_skill("meeting-notes", "references/tags.md")` | The text of that file |
| An unknown skill | `'x' is not one of your skills. Your skills: ...` |
| An unknown file, or a path that leaves the skill | `The skill 'x' has no file '...'. Its files: ...` |

Nothing raises: the agent reads the text and carries on. **The agent decides** whether and when to read a skill;
nothing activates a skill for it. An agent reads only the skills it lists. Skills are not passed on to an agent it
launches: each agent has its own.

**In the log:** the `tools=` of the attempt's `attempt_started` line includes `get_skill`, and each read logs
`skill_read agent=... skill=... resource=...`. Reads are not stored as rows of their own.

The tokens are the usual ones: the instructions the agent reads are tool results, so they are in its input tokens from
then on. That is why a skill's body should stay short (see below).

## The skill model

`Skill` is a frozen Pydantic model. Everything the specification limits is checked when a skill is built; a skill
that breaks a rule raises a `ValidationError` (a `ValueError`).

| Field | Required | Rule |
|---|---|---|
| `name` | yes | 1-64 characters: lowercase letters `a-z`, digits and single hyphens, not starting or ending with a hyphen. `pdf-processing` is valid; `PDF-Processing`, `-pdf` and `pdf--processing` are not |
| `description` | yes | 1-1024 characters. Say what the skill does **and when to use it**; this is all the agent knows before it reads the skill |
| `instructions` | no | The Markdown body of `SKILL.md`. No format is imposed |
| `license` | no | Text |
| `compatibility` | no | 1-500 characters when given: environment requirements |
| `metadata` | no | Text keys to text values |
| `allowed_tools` | no | A space-separated text, written `allowed-tools` in `SKILL.md`. Kept, **not enforced** |
| `resources` | no | The other files: path relative to the skill, and its text. A path must be relative, must not contain `..` and must not be `SKILL.md` |

Methods:
- `skill.read(resource="")`: what `get_skill` returns.
- `Skill.from_markdown(text, resources=None)` and `Skill.from_directory(path)`: see below. Both raise `SkillError`
  (a `ValueError`) for a file that is not a skill, and `ValidationError` for a field that breaks a rule.

Two skills of one agent cannot have the same name.

## Loading a skill

**`Skill.from_directory(path)`** loads a directory in the specification's layout:

```
meeting-notes/
  SKILL.md              required: frontmatter, then the instructions
  references/tags.md    any other files, in any folders
  scripts/  assets/     the conventional folders; no folder is special
```

- `SKILL.md` must start with YAML frontmatter between `---` lines. `name` and `description` are required.
- **The `name` must match the directory name**, as the specification says.
- Every other file below the directory is loaded as a resource, with its path relative to the directory.
- Files that start with a dot (such as `.DS_Store`) are skipped. Files that are not UTF-8 text (images, binaries) are
  skipped and logged as `skill_file_skipped`, because the agent can only read text.
- Frontmatter keys the specification does not define are ignored, because skills written for other tools carry extra
  ones. `metadata` values that YAML reads as numbers (`version: 1.0`) are turned into text.
- A body longer than 500 lines is logged (`skill_long`), as the specification recommends keeping it shorter. It is not
  refused.

**`Skill.from_markdown(text)`** parses the text of one `SKILL.md`, with the resources you give it.

## Reading a skill from somewhere else

A skill whose text lives in a database or an object store is a `Skill` subclass that fetches it when the agent asks. The agent's
`get_skill` tool awaits `skill.aread(resource)`. The default returns `skill.read(resource)`, so a skill that holds its text needs
nothing; a subclass overrides `aread`:

```python
class BucketSkill(Skill):
    etag: str  # changes when the text does

    async def aread(self, resource: str = "") -> str:
        return await bucket.get_text(f"{self.name}/{resource or 'SKILL.md'}")  # your client, awaited: it must not block
```

- The `name` and the `description` are plain fields, because they are in the prompt. Only the instructions and the files are read late.
- **Give the subclass a field that changes when the text does** (an `etag`, a `version`). The agent's hash is made from the fields of its
  skills, and a fetched text is not one of them: without it, an edit in the store leaves the hash as it was, and
  [resuming a job](agents.md#resuming-a-job) reuses results made with the old text.
- A read that fails does not fail the run. The agent is told `The skill 'x' could not be read: <the type of the error>`, and the error
  goes to the log as `skill_read_failed`, because its message can name a bucket or a key.
- featherweight ships no store and no client. Where the skills live, who may edit them and which ones an agent gets is your application's.
  The [spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/skills.md#reading-a-skill-from-somewhere-else) has the rules.

## What is supported

What the specification defines, and what featherweight does with it:

| In the specification | In featherweight |
|---|---|
| `SKILL.md` with `name` and `description` | Supported, with every length and character rule |
| `license`, `compatibility`, `metadata` | Supported: validated, kept, available on the `Skill` |
| `allowed-tools` (experimental) | Parsed and kept; **not enforced**. An agent's tools are only those in its `tools` |
| Markdown body as the instructions | Supported; returned by `get_skill` |
| Other files, `references/`, `assets/` | Supported as **text** files, read on request with `get_skill` |
| `scripts/` | Kept as text for the agent to read. **Nothing runs a script**: featherweight has no code execution |
| Binary assets (images, PDFs) | Not supported: skipped when loading |
| Progressive disclosure, steps 1 to 3 | Supported as described above |
| The name matches the directory | Checked by `from_directory` |
| Instructions under 500 lines and about 5,000 tokens | A line count is logged; the token count is not checked |
| File references one level deep from `SKILL.md` | Not checked |
| Validation with the `skills-ref` tool | Not used; `Skill` checks the same rules itself |

Beyond the specification, in featherweight:
- A skill is a typed Pydantic model that can also be written in code, with no directory.
- An agent can have both `skills` and `subagents` ([launching-agents.md](launching-agents.md)); they are independent.

Not built yet:
- **Finding skills.** An agent knows only the skills it lists. A tool that finds skills by keyword is not built. The matcher
  that finds MCP tools ([tool discovery](https://github.com/aadhithya/featherweight/blob/main/docs/specs/tool-discovery.md#matching)) could serve it.
- Loading skills from an archive or a registry, or sharing a skill between processes.
- Reading a part of a long file, or a size limit on a file.

## Writing a good skill

- **The description is what makes a skill get used.** Say what it does and when: "Use when asked to summarise a
  meeting, a call or minutes", not "Helps with meetings". Include the words a request would use.
- **Keep `SKILL.md` short** and put detail in other files that the instructions name. The agent reads a file only when
  it needs it, so a long reference costs nothing until then.
- **Name the files** in the instructions ("the tags are in `references/tags.md`; read it first"). The agent is told
  which files exist, but not what is in them.
- **Write steps an agent can follow**, with the rules it must keep (a word limit, a format) stated plainly. Test the skill
  by running an agent on a few requests and reading the output, as `examples/skills/agent.py` does.
