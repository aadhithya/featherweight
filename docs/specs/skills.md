# Spec: skills

Status: implemented. How to use it is in [skills.md](../skills.md). The code is [skills.py](../../src/micro/skills.py). It
follows the [Agent Skills specification](https://agentskills.io/specification).

- [Goal](#goal)
- [The model](#the-model)
- [Loading](#loading)
- [How an agent uses a skill](#how-an-agent-uses-a-skill)
- [What is supported](#what-is-supported)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)

## Goal

A skill is a set of instructions an agent reads when it needs them. It is a typed, frozen Pydantic model that checks
everything the specification limits, so a skill that breaks a rule fails when it is built. It costs almost nothing in
context until it is used.

## The model

`Skill` is frozen, and ignores frontmatter keys it does not know (skills written for other tools carry extra ones).

| Field | Rule |
|---|---|
| `name` | Required. 1 to 64 characters: lowercase letters, digits and single hyphens, not starting or ending with a hyphen |
| `description` | Required. 1 to 1024 characters, not blank. Says what the skill does **and when to use it** |
| `instructions` | The Markdown body of `SKILL.md`. No format is imposed |
| `license` | Text |
| `compatibility` | 1 to 500 characters when given |
| `metadata` | Text keys to text values. Numbers in YAML (`version: 1.0`) become text |
| `allowed_tools` | Space-separated text, `allowed-tools` in `SKILL.md`. Kept, **not enforced** |
| `resources` | Other files: a path relative to the skill, and its text. The path must be relative, must not contain `..`, and must not be `SKILL.md` |

A validation failure raises `ValidationError`; a file that is not a skill raises `SkillError`. Both are `ValueError`s. Two
skills of one agent cannot have the same name ([agents](agents.md#checked-when-an-agent-is-built)).

## Loading

- **`Skill(...)`**: a skill written in code, with no directory.
- **`Skill.from_markdown(text, resources=None)`**: parses one `SKILL.md`: YAML frontmatter between `---` lines, then the
  body. No frontmatter, invalid YAML, or a frontmatter that is not a mapping is a `SkillError`.
- **`Skill.from_directory(path)`**: `SKILL.md` and every other file below the directory, as a resource keyed by its
  relative path.
  - The `name` must match the directory name.
  - Files that start with a dot are skipped. Files that are not UTF-8 text are skipped and logged (`skill_file_skipped`).
  - A body over 500 lines is logged (`skill_long`), not refused.

## How an agent uses a skill

Progressive disclosure, in three steps:

| Step | The agent has | How |
|---|---|---|
| 1. Known | The `name` and `description` of every skill | In the system prompt, always |
| 2. Activated | The instructions | `get_skill(name)`, when the agent decides a skill applies |
| 3. Used | Other files of the skill | `get_skill(name, resource=path)`, when the instructions point to a file |

The runner builds `get_skill(name, resource="")` for each attempt of an agent that has skills. It reads only that agent's
skills, and returns text, never raising:

| Call | Returns |
|---|---|
| a known skill | `# Skill: <name>`, the instructions, and a list of the other files |
| a known skill and a file | The text of the file |
| an unknown skill | `'x' is not one of your skills. Your skills: ...` |
| an unknown file, or a path that leaves the skill | `The skill 'x' has no file '...'. Its files: ...` |

The agent decides whether and when to read a skill: nothing activates one for it. Skills are not passed on to the agents
an agent launches. Each read logs `skill_read agent=... skill=... resource=...`. The instructions are tool results, so they
count in input tokens from then on, which is why a body should stay short.

## What is supported

| In the specification | Here |
|---|---|
| `SKILL.md` with `name` and `description` | Supported, with every length and character rule |
| `license`, `compatibility`, `metadata` | Validated and kept |
| `allowed-tools` | Parsed and kept, not enforced. An agent's tools are only those in its `tools` |
| The body as the instructions | Supported |
| Other files, `references/`, `assets/` | Supported as text |
| `scripts/` | Kept as text for the agent to read. Nothing runs a script |
| The name matches the directory | Checked by `from_directory` |
| Instructions under 500 lines | A line count is logged. The 5,000-token advice is not checked |

## Decisions

- **A typed model, also in code.** A skill need not be a directory, and a directory is loaded into the same model.
- **Check what the specification limits, ignore what it does not define.** Skills written for other tools load.
- **The agent decides.** The runner gives it the tool and the prompt, and does not activate a skill on its own.
- **Skills are per agent** and are not inherited by launched agents, so each agent reads what its own definition lists.
- **Text only.** The agent can only read text, so binary files are skipped, and scripts are never run.
- **A long skill is logged, not refused**, as the specification recommends rather than requires.

## Not supported

- Finding skills. An agent knows only the skills it lists. A tool that finds skills by keyword is planned
  ([issue #3](https://github.com/aadhithya/micro/issues/3) and the shared lookup).
- Binary assets (images, PDFs), and scripts that run.
- Loading from an archive or a registry, and sharing a skill between processes.
- Reading a part of a long file, or a size limit on a file.
- Enforcing `allowed-tools`, checking references one level deep, and the `skills-ref` validator.

## Tests

[test_skills.py](../../tests/test_skills.py) covers the model's rules, `from_markdown`, `from_directory` (names,
hidden and binary files, resources), `read`, the prompt, and the `get_skill` tool. A real-model test is in
[tests/e2e/test_skills_e2e.py](../../tests/e2e/test_skills_e2e.py).
