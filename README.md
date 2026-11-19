# featherweight

**An opinionated agent harness. Pydantic is the spec. Everything else follows.**

An agent is a typed class. Its input, output, skills and subagents are declared right there and enforced by Pydantic, not
by convention, and not by hoping the model behaves. The boilerplate every agent project ends up writing (retries, limits,
failures, tracing, tools, delegation) is already written. You write the pipeline. That's the part that's yours.

A malformed agent never reaches the model. A running one has a known input and a known output, because both are typed and
both are enforced. No guessing.

It has opinions. Take them, or fork off.

> Designed by a human. Implemented by Claude.

## What you get

| | |
|---|---|
| **Typed, end to end** | What goes in and what comes out is a schema. A bad answer is caught, sent back to the model and retried before your code ever sees it. |
| **Declared, not assembled** | An agent is a class. A bad declaration fails when it is written, not in production at 3 a.m. |
| **Failures are values** | A failed run hands you the error and a status. No try/except pyramid. |
| **Plain Python pipelines** | Order, loops and branching are `if` and `for`. No graph to draw, and no pretending to be a workflow engine. |
| **Subagents on a leash** | Agents delegate to agents, but only the ones they are allowed to, and only so deep and so wide. |
| **Resume instead of rerun** | A pipeline that dies halfway picks up where it stopped, without paying for the steps that worked. |
| **Run it in the background** | Submit from your API, run on a worker, follow it live, cancel it. The two sides never talk to each other directly. |
| **Streams to your UI** | Live job status and chat answers stream over standard protocols, so a browser can listen as is. |
| **Any model, any tool** | Gemini, Gemma, Claude, OpenAI or your own. MCP servers and skills plug in and stay out of the context until needed. |
| **Your data is yours** | The framework stores metadata and nothing else: status, tokens, timing. Everything else is handed back to you, and how it gets stored is your call. |
| **Your storage, your call** | Where and how things are stored is up to you. Implement the interface, and we don't care what is behind it. sqlite and Postgres are there to get you going. |

## Why does this exist? Pydantic AI and LangGraph are right there.

They are. Use them. This is built on LangChain and LangGraph anyway.

- **Boredom.** Every agent project needs the same retries, limits and MCP wiring. I wrote them once and got on with my day.
- **Mostly background agents.** Pipelines, batches, fan-out: nobody is watching the tokens. A chat agent streams its answer
  ([streaming agents](https://github.com/aadhithya/featherweight/blob/main/docs/streaming.md)), but there is no UI, and no stored conversations.
- **Very opinionated.** Sorry, not sorry. Don't like them? Fork off.

Want durable execution, evals or OpenTelemetry? Pydantic AI. Want a graph that pauses and resumes? LangGraph.

### Next to LangChain

The same agent, one that finds the people in a text.

```python
# LangChain: you assemble it, and the schema only shapes the answer
from langchain.agents import create_agent
from pydantic import BaseModel


class People(BaseModel):
    people: list[str] = []


agent = create_agent(
    "google_genai:gemini-3.8-flash",
    system_prompt="List every person named in the text.",
    response_format=People,
)
result = await agent.ainvoke({"messages": [("user", text)]})
people = result["structured_response"]  # retries, limits, a status and a run log: write them yourself
```

```python
# featherweight: you declare it, and the runner does the rest
from typing import ClassVar

from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, RunStatus


class ExtractInput(AgentInput):
    text: str


class ExtractOutput(AgentOutput):
    people: list[str] = []


class PeopleExtractor(AgentBase):
    name: str = "people_extractor"
    description: str = "Finds the people named in a text"
    version: str = "0.1.0"
    prompt: str = "List every person named in the text."
    input_model: ClassVar[type[AgentInput]] = ExtractInput
    output_model: ClassVar[type[AgentOutput]] = ExtractOutput

    def check_output(self, agent_input, output):  # what a schema can't say; raise and the model tries again
        if missing := [p for p in output.people if p not in agent_input.text]:
            raise ValueError(f"Not in the text: {missing}")


result = await AgentRunner(db).run(PeopleExtractor, ExtractInput(text=text), job_id=job_id)
if result.status == RunStatus.SUCCESS:
    print(result.output.people)  # validated, checked, and retried until it is right
```

What differs: the agent is declared, not assembled. A bad declaration fails when the class is built, a bad answer fails
inside the run and is retried, and every attempt lands in a run log. LangChain is the better pick for an open-ended chain of
tools. This is for the agents that run in a pipeline.

## Getting started

```
pip install featherweight
pip install 'featherweight[anthropic]'    # or mcp, openai, postgres
```

Python 3.12 or later. Import what you use: `from featherweight import AgentBase, AgentRunner`. (`import featherweight as ftw`
works too, as a joke.) [Getting started](https://github.com/aadhithya/featherweight/blob/main/docs/getting-started.md) has a first agent and how to run it.

**With a coding agent.** There is a skill that teaches a coding agent to build with `featherweight`. In Claude Code:

```
/plugin marketplace add aadhithya/featherweight
/plugin install featherweight@featherweight
```

Other tools that read Agent Skills: copy [`skills/featherweight`](https://github.com/aadhithya/featherweight/blob/main/skills/featherweight) into their skills folder.

## Documentation

- [**Getting started**](https://github.com/aadhithya/featherweight/blob/main/docs/getting-started.md): install it, write an agent, run it
- [**Docs**](https://github.com/aadhithya/featherweight/blob/main/docs/README.md): the guides, and where to begin
- [**Specs**](https://github.com/aadhithya/featherweight/blob/main/docs/specs/README.md): what each part does, and why
- [**Examples**](https://github.com/aadhithya/featherweight/blob/main/examples/README.md): code you can run
- [**Development**](https://github.com/aadhithya/featherweight/blob/main/docs/development.md): the commands, the layout, contributing

## License

Apache License 2.0. See [LICENSE](https://github.com/aadhithya/featherweight/blob/main/LICENSE).
