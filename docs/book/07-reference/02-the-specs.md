<!-- nav:top -->
[Book](../README.md) › [Part 7: Reference](../README.md#part-7-reference) › The specs
<!-- /nav:top -->

# The specs

Know when to read a spec.

A spec says **exactly what a part does, why it was decided that way, and what is left out**. The book teaches you to build, a guide shows how to use a
feature, and a spec is for relying on it, changing it, or finding out why it behaves as it does. If a guide and a spec disagree, the spec and the code are right.

There is one spec for each part: agents, the runner, models, jobs and tracing, submitted jobs, background agents, persistence, observers, streaming, tools,
MCP, tool discovery and skills. [The index](../../specs/README.md) lists them.

**Where things belong.** A change goes in the spec of the thing it changes, and not in the spec of whatever work happened to touch it. Each fact is written once,
in the spec that owns it, and the others link to it. [The table of who owns what](../../specs/README.md#where-things-belong) is the rule.

| You ask... | Read |
|---|---|
| Why does a failed job not retry? What happens when a worker dies? | [Submitted jobs](../../specs/submitted-jobs.md) |
| What exactly is stored, and what never is? | [Persistence](../../specs/persistence.md) |
| What is in a span, and which attribute names are used? | [Observers and OpenTelemetry](../../specs/observability.md) |
| How does the runner retry, limit and launch agents? | [The agent runner](../../specs/runner.md) |

> **Why** the book does not restate them: a fact written twice is soon written two ways.

<!-- nav:bottom -->
---

[← The guides](01-the-guides.md) · [Part 7: Reference](../README.md#part-7-reference) · [The glossary →](03-the-glossary.md)
<!-- /nav:bottom -->
