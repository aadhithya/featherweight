# Skills

Status: implemented. How to use it is in [skills.md](../skills.md). The code is [skills.py](../../src/featherweight/skills.py). It
follows the [Agent Skills specification](https://agentskills.io/specification).

- [Goal](#goal)
- [The model](#the-model)
- [Loading](#loading)
- [How an agent uses a skill](#how-an-agent-uses-a-skill)
- [Reading a skill from somewhere else](#reading-a-skill-from-somewhere-else)
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
skills (with `await skill.aread(resource)`, see [below](#reading-a-skill-from-somewhere-else)), and returns text, never raising:

| Call | Returns |
|---|---|
| a known skill | `# Skill: <name>`, the instructions, and a list of the other files |
| a known skill and a file | The text of the file |
| an unknown skill | `'x' is not one of your skills. Your skills: ...` |
| an unknown file, or a path that leaves the skill | `The skill 'x' has no file '...'. Its files: ...` |

The agent decides whether and when to read a skill: nothing activates one for it. Skills are not passed on to the agents
an agent launches. Each read logs `skill_read agent=... skill=... resource=...`. The instructions are tool results, so they
count in input tokens from then on, which is why a body should stay short.

## Reading a skill from somewhere else

A skill whose text lives in a database or an object store (S3) is a `Skill` subclass that fetches the text when the agent asks for it. The
extension point is the read:

| Method | |
|---|---|
| `read(resource="")` | Synchronous. The instructions (with the other files listed), or the text of one file, from the fields of the skill |
| `aread(resource="")` | Asynchronous, with the same arguments and the same result. **The default calls `read`**, so a skill that holds its text needs nothing. The runner's `get_skill` always awaits `aread` |

A subclass that fetches overrides `aread`, and never blocks the event loop (every agent of the process shares it).

- **A skill is still data and still a valid `Skill`:** its name, description and the rest are checked as always, and a description is what is in
  the prompt, so it is not fetched. Only the instructions and the files are read late.
- **The hash.** The agent's hash is made from the fields of its skills. A fetched text is not a field, so **a subclass adds one that changes
  when the text does** (an `etag` or a `version`). Without it, an edit in the store leaves the hash as it was, and
  [resuming a job](../jobs.md#resuming-a-job) reuses results made with the old text.
- **A read that fails is text, not an exception.** If `aread` raises, the runner logs `skill_read_failed` (with the error) and the agent gets
  `The skill 'x' could not be read: <the type of the error>`, so the run goes on, as with the other tools that return their errors. The message of the error
  goes to the log only: it can name a bucket or a key.
- **Nothing is fetched until the agent asks,** and the runner caches nothing: caching for the length of a run is the subclass's.
- **The framework ships no store.** There is no S3 or database client in featherweight. The recipes show a skill from a database and a skill
  from S3.

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
- **The read is async-capable, and the store is not ours.** `aread` is the one hook, and `read` stays for code that holds the text. A URL is not a
  resource: the model cannot open one, so the framework would have to fetch it, with a client, limits and the risks that come with it. The
  path stays the name the model sees, and where the text comes from is the subclass's business.
- **A long skill is logged, not refused**, as the specification recommends rather than requires.

## Not supported

- Finding skills. An agent knows only the skills it lists. A tool that finds skills by keyword is not built. The BM25
  matcher of [tool discovery](tool-discovery.md#matching) is generic and could serve it.
- Binary assets (images, PDFs), and scripts that run.
- Loading from an archive or a registry that we ship, and sharing a skill between processes. `aread` is the hook, and the recipes show how.
- A skill list that changes at run time. An agent's `skills` is fixed when its class is defined; a platform that builds an agent for each request
  from its own database rows builds the class then.
- Reading a part of a long file, or a size limit on a file.
- Enforcing `allowed-tools`, checking references one level deep, and the `skills-ref` validator.

## Tests

[test_skills.py](../../tests/test_skills.py) covers the model's rules, `from_markdown`, `from_directory` (names,
hidden and binary files, resources), `read`, the prompt, and the `get_skill` tool. `aread` is covered with a subclass that reads from a fake
in-memory store: the default equals `read`, the tool awaits it, the hash changes with the subclass's `etag`, and a read that raises comes
back as text and is logged. A real-model test is in
[tests/e2e/test_skills_e2e.py](../../tests/e2e/test_skills_e2e.py).
