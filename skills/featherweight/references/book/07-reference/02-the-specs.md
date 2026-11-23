<!-- Copied from docs/book/07-reference/02-the-specs.md by scripts/build_skill.py. Edit the original, not this file. -->

# The specs

Know when to read a spec.

A spec says **exactly what a part does, why it was decided that way, and what is left out**. The book teaches you to build, a guide shows how to use a
feature, and a spec is for relying on it, changing it, or finding out why it behaves as it does. If a guide and a spec disagree, the spec and the code are right.

There is one spec for each part: agents, the runner, models, tools, skills, identity, streaming, attachments, the run log, jobs, submitted jobs, background jobs and observers (MCP and tool discovery have their own). [The index](https://github.com/aadhithya/featherweight/blob/main/docs/specs/README.md) lists them.

**Where things belong.** A change goes in the spec of the thing it changes, and not in the spec of whatever work happened to touch it. Each fact is written once,
in the spec that owns it, and the others link to it. [The table of who owns what](https://github.com/aadhithya/featherweight/blob/main/docs/specs/README.md#where-things-belong) is the rule.

| You ask... | Read |
|---|---|
| Why does a failed job not retry? What happens when a worker dies? | [Submitted jobs](https://github.com/aadhithya/featherweight/blob/main/docs/specs/submitted-jobs.md) |
| What exactly is stored, and what never is? | [Persistence](https://github.com/aadhithya/featherweight/blob/main/docs/specs/persistence.md) |
| What is in a span, and which attribute names are used? | [Observers and OpenTelemetry](https://github.com/aadhithya/featherweight/blob/main/docs/specs/observability.md) |
| How does the runner retry, limit and launch agents? | [The agent runner](https://github.com/aadhithya/featherweight/blob/main/docs/specs/runner.md) |
| Who is a run for, and what does featherweight do and not do with that? | [Identity](https://github.com/aadhithya/featherweight/blob/main/docs/specs/identity.md) |

> **Why** the book does not restate them: a fact written twice is soon written two ways.
