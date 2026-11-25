# Agents built from JSON, with your own rules

Your users define agents in a form; you keep the definition as JSON and build the class when it is needed. The builder is yours, so the rules are too:
which tools a user may pick, which models, whether they may add skills, what every agent gets.

```python
# yours: definition_from_your_database, house_style_md (the text of a skill) and runner (an AgentRunner)
from typing import ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import Field, create_model

from featherweight import AgentBase, AgentInput, AgentOutput, SetupError, Skill
from featherweight.model_settings import Gemini, ModelSettings

HOUSE_STYLE = Skill.from_markdown(house_style_md)  # injected into every agent: the users do not choose it
MODELS: dict[str, ModelSettings] = {"fast": Gemini()}  # the models a user may pick, by a name


@tool
def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


TOOLS: dict[str, BaseTool] = {"word_count": word_count}  # the catalog: the only tools a user can give an agent
TYPES = {"string": str, "integer": int, "boolean": bool}


def fields(base: type, name: str, spec: list[dict], default) -> type:
    return create_model(name, __base__=base, **{f["name"]: (TYPES[f["type"]], default(f)) for f in spec})


def build(definition: dict) -> type[AgentBase]:
    """A user's definition, as an agent class. What it may not contain is refused here, before anything runs."""
    if definition.get("skills"):
        raise SetupError("users cannot add skills")
    unknown = [t for t in definition.get("tools", []) if t not in TOOLS]
    if unknown:
        raise SetupError(f"not in the catalog: {unknown}")
    title = "".join(part.title() for part in definition["id"].split("_"))
    In = fields(AgentInput, f"{title}Input", definition["input"], lambda f: ...)
    Out = fields(AgentOutput, f"{title}Output", definition["output"], lambda f: TYPES[f["type"]]())
    cls = create_model(
        title,
        __base__=AgentBase,
        name=(str, definition["id"]),  # your unique key: it is the job's name on a worker
        description=(str, definition["description"]),
        version=(str, str(definition["version"])),
        prompt=(str, definition["prompt"]),
        model=(ModelSettings, Field(default_factory=lambda: MODELS[definition["model"]])),
        tools=(list[BaseTool], Field(default_factory=lambda: [TOOLS[t] for t in definition.get("tools", [])])),
        skills=(list[Skill], Field(default_factory=lambda: [HOUSE_STYLE])),
    )
    cls.input_model, cls.output_model = In, Out
    return cls


Summariser = build(definition_from_your_database)
result = await runner.run(Summariser, Summariser.input_model(text="..."))
```

On a worker, the agent does not exist in the code that started it. `jobs_lookup` is the fallback that finds it, from the same definitions:

```python
# yours: definitions (your storage of definitions), db, queue and runner; Teacher is the identity, as in the identity recipe
from featherweight import JobWorker


async def jobs_lookup(
    name: str, job_hash: str | None, identity: Teacher
):  # the worker declares `Teacher`, so it is never None
    definition = await definitions.get(identity.tenant, name)  # your storage; the identity scopes the search
    return build(definition) if definition else None


worker = JobWorker(db, queue, runner, jobs=[], jobs_lookup=jobs_lookup, identity_type=Teacher)
```

<!-- skill:omit -->
- **A definition is data, and the class is made from it.** Pydantic validates it as it validates a class you wrote: a malformed definition fails when it is built, before any model is called.
- **Your rules are in `build`:** a catalog of tools (a name the user picked is looked up, never imported), a list of models, no user skills, a mandatory skill. featherweight has no builder and no spec format on purpose.
- **Do not let users declare MCP servers of the stdio kind:** `StdioMCPServer` runs a command on your server. Allow `HttpMCPServer`, or servers from a list you control.
- **The name is the job's key on a worker,** so it is yours to keep unique in what the lookup searches and stable ([names](../specs/identity.md#the-developers-responsibilities)). It must match `[A-Za-z][A-Za-z0-9_.-]*`.
- **The lookup has a timeout** (`FTW_JOBS_LOOKUP_TIMEOUT`, 15 seconds): one that hangs fails its job, and must be safe to cancel. **A name that is not a job name never reaches it.**
- **The worker checks the answer:** the name must be the one asked for, and the job's hash must be the one recorded when the job was submitted, so an agent edited while its job waited is not run as the new definition
  ([jobs that are not declared in code](../submitted-jobs.md#jobs-that-are-not-declared-in-code)). The identity scopes the search; whether this user may run this agent is your check, before `submit`.
<!-- /skill:omit -->
