# micro

A tiny wrapper for writing agents, and one way to build a pipeline of them.

Writing agents is still fuzzy: there is no obvious, simple way to define one and know it behaves. `micro` is a small
answer. An agent is a Pydantic class (prompt, model, tools, input and output schemas), so **type checks and the agent's
specification are enforced for free**: Pydantic validates what goes in and what comes out. The runner adds retries,
a concurrency limit, failures returned as results, and a trace of every attempt in sqlite.

If you want to build a pipeline of agents, this is one way to start with the boring parts already done. It is about
1,000 lines, built on langchain and langgraph, async throughout, Gemini models for now.

> A small, quick, vibe coded project: a few days of work. A prototype with limited scope, not production software.

## An agent

```python
from typing import ClassVar

from micro.models import AgentBase, AgentInput, AgentOutput


class ExtractInput(AgentInput):
    text: str


class ExtractOutput(AgentOutput):  # also carries `open_questions: list[str]`
    people: list[str] = []


class PeopleExtractor(AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"
    prompt: str = "List every person named in the text."
    input_model: ClassVar[type[AgentInput]] = ExtractInput
    output_model: ClassVar[type[AgentOutput]] = ExtractOutput

    def check_output(self, agent_input, output):  # rules a schema cannot express; raise to reject and retry
        missing = [p for p in output.people if p not in agent_input.text]
        if missing:
            raise ValueError(f"names not in the text: {missing}")
```

```python
import asyncio

from micro.db import Database
from micro.models import RunStatus
from micro.runner import AgentRunner


async def main():
    async with Database("micro.db") as db:
        job_id = await db.create_job("demo")
        result = await AgentRunner(db).run(
            PeopleExtractor, ExtractInput(text="Anna Berger met Tom Okafor on Monday."), job_id=job_id
        )
        if result.status == RunStatus.SUCCESS:
            print(result.output.people)
        else:
            print("failed:", result.error)  # runtime failures are returned, not raised


asyncio.run(main())
```

Set `GOOGLE_API_KEY` first. `runner.run_many(Agent, inputs, job_id=...)` runs one agent over many inputs at once.

## What you get

- **Typed, static agents.** Input, output, tools and prompt are declared; Pydantic enforces them.
- **Failures are results.** `AgentResult(status=FAILED, error=...)`, not exceptions, so the pipeline decides what a
  failure means. Wrong input types and unknown job ids are programming errors and do raise.
- **Retries** (3 attempts by default), except for errors that cannot succeed, such as a bad key. A retry resamples with
  the same input; the error is logged but not sent back to the model.
- **Output checks:** pydantic validators on the output model, plus `check_output` against the input.
- **Concurrency limit** (`MAX_CONCURRENT_AGENT_RUNS`, default 10) so fan-out stays within rate limits.
- **Tracing.** Every attempt is a row in sqlite (input, output, status, error, tokens, latency, model, agent version,
  system prompt hash), grouped by job. Pipelines run as jobs with `run_job`, which records `SUCCESS` or `FAILED` with
  the error. Query a run with `sqlite3 micro.db "select agent_name, attempt, status, error from agent_runs where job_id='...'"`.
- **Tools** per agent, and a few ready-made ones: `calculate` (exact arithmetic), date tools, and `store_get` /
  `store_set` for state shared between the agents of a job (`RunStore`, in memory; any Redis client can replace it).
- **Open questions:** an agent can report questions and conflicts instead of guessing (`allow_user_questions`).

**Why sqlite for tracing, and not OpenTelemetry or Langfuse?** To keep it as small and simple as possible: two tables,
no collector, no account. A real deployment will want a proper backend; every attempt passes through one loop in
`AgentRunner.run` and every pipeline through `run_job`, so that is a change in two places.

## An example pipeline: `doclearn`

[`src/doclearn`](src/doclearn) is a worked example on top of the harness: learn the template of a document family from a
few examples, then write a new document from a few facts. It has two pipelines and five agents (Analyzer,
Reconciliation, Semantic Alignment, Generator, Form Filler), with code checks between them. It is not specific to any
domain; the samples are project proposals and incident reports in [`examples/`](examples).

```
cp .env.example .env     # set GOOGLE_API_KEY
make build
make pipeline ARGS="examples/project_proposals --category project_proposals"
```

(Without Docker: `uv sync`, then `PYTHONPATH=src uv run python -m doclearn.cli examples/project_proposals --category project_proposals`.)

It learns from the example documents, shows what it learned, fills a form (by hand, or an agent invents a case) and
generates a document. Everything lands in `runs/<time>_<category>/` and in `micro.db`.

Read [`pipeline.py`](src/doclearn/pipeline.py) and [`learning.py`](src/doclearn/learning.py) for how a pipeline is an
ordinary async function over `runner.run`, with the decisions about failure in code, and
[`generation.py`](src/doclearn/generation.py) for an agent with a check tool.

## Limits

A prototype, and the example is a quick demonstration, not a finished product. Deliberately out of scope:

- **No evaluation.** The tests show it works; there is no held-out set, prompt comparison or quality metric. The
  conformance score the example prints measures self-consistency, not quality.
- **No personal-data handling.** Documents go to the model as they are and are kept in plaintext in the run log. The
  examples are invented; do not run it on real client documents.
- **Generation is lenient.** Fixed text is checked by similarity (85%), so a small but meaningful edit can pass, and
  totals are computed by the model, not re-checked. Free-written blocks are not verified.
- **Gemini only,** markdown only, and learning from a user's edits is designed but not built.

The places a production system would plug in exist: `AgentRunner(model_factory=...)` for another model, the single
attempt loop for tracing, `RunStore` for Redis, and pipelines as plain async functions that an API can start. The rest
(a real database, auth, cost accounting, evaluation) is work still to do.

## Development

| Command | |
|---|---|
| `make build` | Build the image |
| `make test` | Unit tests (the model is mocked, no key needed); or `uv run pytest` |
| `make e2e` | Tests against the real model (costs money) |
| `make pipeline ARGS="..."` | Run the example command line |

```
src/micro/      the harness
src/doclearn/   the example pipelines
examples/       sample documents
tests/          mocked tests; tests/e2e uses the real model
```

Issues and pull requests are welcome; run `make test` first.

## License

Apache License 2.0. See [LICENSE](LICENSE).
