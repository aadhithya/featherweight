<!-- nav:top -->
[Book](../README.md) › [Part 1: Your first agent](../README.md#part-1-your-first-agent) › Choosing a model  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# Choosing a model

Choose the model: it is a setting of the agent. This is the assistant that the next parts build on.

<!-- code: code/1-6/app.py -->
```python
"""Chapter 1-6: the model is a setting of the agent. This is the assistant that the next parts build on."""

import asyncio
import sys
from pathlib import Path
from typing import ClassVar, Literal

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    default_db_path,
)


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
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")  # the one line to change
    input_model: ClassVar[type[AgentInput]] = Report  # the contract is set on the class: the same for every run
    output_model: ClassVar[type[AgentOutput]] = Summary

    def check_output(self, agent_input, output):  # a rule the schema cannot say: raise to reject the attempt
        if output.service and output.service.lower() not in agent_input.text.lower():
            raise ValueError(f"The report does not name the service {output.service!r}")


async def main(path: str = "reports/01-checkout-outage.txt") -> None:
    report = Report(text=Path(path).read_text())
    async with SqliteDatabase(default_db_path()) as db:
        job_id = await db.create_job("incident-report")  # a job id groups the runs of one piece of work
        # the class, not an instance: the runner builds the agent
        result = await AgentRunner(db).run(IncidentSummariser, report, job_id=job_id)
    print(result.status.value, result.model_name, result.output or result.error)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
```

```python
model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low")
```

The models of a provider are in its class: `docker compose run --rm --no-deps book python -c "from featherweight import Gemini; print(list(Gemini.models))"`.

**What to notice**

- **The settings are checked when the agent is built.** A model that does not exist, or an effort that model does not accept
  (`Gemini(model="gemini-3.8-flash", thinking="minimal")`), fails at once with a message that says why.
- **The settings are part of the run log**, so two runs can be told apart by the model that made them.
- **This file is the project's base.** Later chapters keep `Report`, `Summary` and `IncidentSummariser`, and add one thing at a time.
- **The providers are `Gemini`, `Gemma`, `Claude` and `OpenAI`:** each is a class with the controls of its provider ([the providers](../../models.md#the-providers)).
- **Your own provider is a subclass.** Any provider that LangChain has a chat model for works: subclass `ModelSettings` and write `build()`.
  [Add a provider](../../models.md#add-a-provider-ollama) shows it for Ollama.

> **Why a setting.** The model is part of what the agent is, so it is declared and checked like the rest. See [the model spec](../../specs/models.md).

Next: [Part 2](../02-abilities/01-tools.md) gives the assistant abilities.

<!-- nav:bottom -->
---

[← Failures and check_output](05-failures-and-check-output.md) · [Part 1: Your first agent](../README.md#part-1-your-first-agent) · [Tools →](../02-abilities/01-tools.md)
<!-- /nav:bottom -->
