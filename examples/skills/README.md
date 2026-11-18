# Skills

An `Assistant` agent with three skills: meeting notes, email replies and release notes. It shows an agent reading a
skill, and the files the skill points to, only when a request needs them.

```
make example-skills                      # three sample requests, one for each skill
make example-skills ARGS="your request"
```

Without Docker: `uv run python examples/skills/agent.py "your request"`.

## What happens

The agent's prompt lists each skill by name and description, and nothing else. When a request fits a skill, the agent
calls `get_skill` to read its instructions, and then reads the files those instructions name. The script prints each
read:

```
> Reply to this email from Ms Okonkwo, a client: ...
  [read] email-reply: instructions
  [read] email-reply: references/tone.md
Dear Ms Okonkwo, ...
Kind regards,
```

The formal greeting and sign-off come from `references/tone.md`: the skill's instructions say to read it, but not what
is in it.

## The skills

Each folder here with a `SKILL.md` is a skill, in the [Agent Skills](https://agentskills.io/specification) format:

| Skill | Other files | What it does |
|---|---|---|
| [`meeting-notes`](meeting-notes) | `references/tags.md` | Turns meeting text into short notes, each with a tag from the file |
| [`email-reply`](email-reply) | `references/tone.md` | Writes a short reply, with the greeting and sign-off of the right tone |
| [`release-notes`](release-notes) | none | Turns a list of changes into release notes grouped as New, Improved and Fixed |

## Things to try

- Add a skill: make a folder with a `SKILL.md` (its `name` must match the folder name) and run again; the agent picks up
  every skill folder next to `agent.py`.
- Ask for something no skill covers: the agent should not read any.

## Where to look

[`agent.py`](agent.py) is the agent and the script. What a skill is, how it is loaded and which parts of the
specification are supported is in [docs/skills.md](../../docs/skills.md).
