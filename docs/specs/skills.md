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
| an unknown file | `The skill 'x' has no file '...'. Its files: ...` |
| a path that is not inside the skill ([below](#the-path-the-model-chooses)) | `The skill 'x' has no file '...'.` The skill is not asked, and nothing is raised or logged as an error |

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
- **`aread` returns text.** What it returns is the tool's result, so a value that is not a `str` (a store client that answers `None` for a key that does not exist, or `bytes`) is a read that failed: the same text
  as an exception (`could not be read: TypeError`), and `skill_read_failed` in the log. An empty string is text.
- **`aread` has a timeout:** `FTW_SKILL_READ_TIMEOUT` seconds (15 by default; [configuration](configuration.md)), read when the runner is built, with no argument and no way to switch it off. A read that takes longer is cancelled and is a read
  that failed (`could not be read: TimeoutError`), so a hung store does not hold the attempt, and its concurrency slot, for ever. An `aread` must be safe to cancel.
- **`aread` can ask who the run is for.** The identity of the run is `CURRENT_IDENTITY` (`featherweight.context`), the same context variable that code which cannot take `runtime` reads ([identity](identity.md#how-tools-read-it)): a subclass that
  keeps skills per tenant can check what the identity may fetch. It is not an argument of `aread`, so the signature is the same for every skill. featherweight does not check it: a read is a tool call, and a tool checks authorization itself.
- **Nothing is fetched until the agent asks,** and the runner caches nothing: caching for the length of a run is the subclass's.
- **The framework ships no store.** There is no S3 or database client in featherweight. The recipes show a skill from a database and a skill
  from S3.

### The path the model chooses

`resource` is written by the model, and a model reads documents and user input, so a prompt injection can choose it. The runner therefore gives `aread` only a **normalised path that is inside the skill**, or the empty string, and
answers anything else itself, as text, without asking the skill:

- **Refused, flatly:** a path that starts with `/`, one with a backslash, one with a `..` segment (it is not resolved: `a/../b` is refused too), one with a control character (NUL and newline included), one longer than 256 characters, and one that is
  only `.` and `/`. The answer is `The skill 'x' has no file '...'.` (the path, cut at 80 characters), the same as for a file that is not there, so the model learns nothing about what is outside. It is not an error: the run goes on.
- **Normalised:** a `.` segment is dropped and `//` is one `/`, so `./references/a.md` and `references//a.md` are `references/a.md`.
- **Not decoded:** the runner does not URL-decode. A subclass that does (an HTTP store) treats what it gets as untrusted, and checks it again. What a file holds is not checked here.

The default `read` is unchanged: it looks the path up in the skill's own files. The rule is for every skill, because a subclass's `aread` replaces that lookup and with it the check.

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
- **The runner checks the model's path, and the skill still treats it as untrusted.** The check is in one place (`clean_resource`), so that a subclass that joins paths, reads a directory or calls a service that normalises `..` cannot be reached by a path that leaves the skill.
- **A read is bounded and returns text.** A fetching skill is application code in the middle of an attempt: a timeout and a type check stop a store that hangs or answers with something else from reaching the model or stalling the run.
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
back as text and is logged; a value that is not text, and a read that takes longer than the timeout, come back as text too; a path that is absolute, has a `..`, a backslash, a control character or is too long never reaches `aread`
and a normal one reaches it normalised; an `aread` sees the identity of the run. A real-model test is in
[tests/e2e/test_skills_e2e.py](../../tests/e2e/test_skills_e2e.py).
