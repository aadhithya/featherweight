<!-- nav:top -->
[Book](../README.md) › [Part 1: Your first agent](../README.md#part-1-your-first-agent) › A typed agent  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# A typed agent

Write an agent as a typed class: its input, its output and its prompt.

<!-- code: code/1-2/app.py -->
```python
"""Chapter 1-2: the agent, as a typed class. Nothing is run against a model."""

from typing import ClassVar, Literal

from featherweight import AgentBase, AgentInput, AgentOutput


class Report(AgentInput):  # what goes in: a typed model, checked before the model is called
    text: str


class Summary(AgentOutput):  # also carries `open_questions: list[str]`
    title: str = ""  # every field has a default: a model that does not know is not forced to invent
    # only these values, or None when the report does not say
    severity: Literal["low", "medium", "high"] | None = None
    service: str | None = None
    impact: str = ""
    next_steps: list[str] = []


class IncidentSummariser(AgentBase):
    name: str = "incident_summariser"  # how the run log and the traces name the agent
    description: str = "Turns an incident report into a short, structured summary"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "Summarise the incident report. Use only what the report says. "
        "If the severity or the service is not stated, leave it out and ask in open_questions."
    )
    input_model: ClassVar[type[AgentInput]] = Report  # the contract is set on the class: the same for every run
    output_model: ClassVar[type[AgentOutput]] = Summary


if __name__ == "__main__":
    agent = IncidentSummariser()  # a malformed agent is refused here, before any model is involved
    print(agent.name, agent.version)
    print(list(Summary.model_fields))
```

```text
incident_summariser 1
['open_questions', 'title', 'severity', 'service', 'impact', 'next_steps']
```

**What to notice**

- **The input and the output are Pydantic models.** The runner checks both, so a summary with `severity="urgent"` is never handed to your code.
- **The output fields have defaults.** If the report does not say, the model can leave a field empty and ask in `open_questions`, which
  `AgentOutput` carries, and does not have to invent an answer to pass the check.
- **The class is checked when it is built.** `IncidentSummariser()` refuses a malformed agent (a model that does not exist, a `recursion_limit` out of
  range, duplicate tool names) before any model is involved.
- `name`, `description` and `version` identify the agent in the run log: change the version when the prompt or the behaviour changes.

> **Why typed.** Pydantic is the spec: what goes in and what comes out is a schema, enforced, and not a convention. See
> [the philosophy](../00-start-here/03-philosophy.md) and [the agent spec](../../specs/agents.md).

More on defining agents: [agents guide](../../agents.md#defining-an-agent).

Next: run it.

<!-- nav:bottom -->
---

[← Install and set up](01-install-and-set-up.md) · [Part 1: Your first agent](../README.md#part-1-your-first-agent) · [Running it →](03-running-it.md)
<!-- /nav:bottom -->
