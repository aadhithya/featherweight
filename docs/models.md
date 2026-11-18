# Models and providers

An agent's `model` says which model runs it and how hard it thinks. Each provider has its own class, because the controls
differ: a thinking level, an effort, an on/off switch.

```python
from micro.model_settings import Claude, Gemini, OpenAI


class Triager(AgentBase):
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low")
```

The reasons and the details are in the [spec](specs/models.md). This page is how to use it.

- [The providers](#the-providers)
- [Install and keys](#install-and-keys)
- [What is checked](#what-is-checked)
- [No temperature](#no-temperature)
- [Add a model: GPT-5.x](#add-a-model-gpt-5x)
- [Add a provider: Ollama](#add-a-provider-ollama)
- [Tokens and the run log](#tokens-and-the-run-log)
- [Limits](#limits)

## The providers

| Class | Models | Control | Default |
|---|---|---|---|
| `Gemini` | `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite` | `thinking`: `minimal`, `low`, `medium`, `high` (`3.8-flash` has no `minimal`) | `gemini-3.5-flash-lite`, `medium` |
| `Gemma` | `gemma-4-26b-a4b-it`, `gemma-4-31b-it` | `thinking`: `minimal` (off) or `high` | `gemma-4-26b-a4b-it`, `minimal` |
| `Claude` | `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-2` | `effort`: `low`, `medium`, `high`, `xhigh`, `max`. `thinking`: `adaptive`, or `off` where allowed | `claude-haiku-5-5`, `medium` |
| `OpenAI` | `gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6.2-sol`, `gpt-6.2-astra` | `effort`: `none`, `low`, `medium`, `high`, `xhigh`, `max`, as each model allows | `gpt-6-luna`, `medium` |

Every class also has `max_output_tokens`, the most the model may produce, **thinking included**. Claude needs it, so it
defaults to 16,000 there: raise it at `xhigh` and `max`.

An agent with no `model` uses `Gemini()`.

## Install and keys

Gemini and Gemma come with `micro`. The other two are extras:

```
pip install 'micro[anthropic]'     # ANTHROPIC_API_KEY
pip install 'micro[openai]'        # OPENAI_API_KEY
```

A package that is missing, or a key that is not set, fails the run at once with a message that says what to do, and is not
retried.

## What is checked

When the agent is defined:

- the model must be one of the class's models, and the message lists them;
- the effort must be one that **that model** accepts: `gemini-3.8-flash` has no `minimal`, and `gpt-6.2-sol` does not accept
  `none`;
- `Claude(thinking="off")` is accepted only for a model that allows it (`claude-haiku-5-5`), and only at `high` effort or
  below;
- a field the class does not have is an error.

```python
Gemini(model="gemini-3.8-flash", thinking="minimal")
# ValidationError: gemini-3.8-flash does not accept thinking='minimal'. It accepts: low, medium, high
```

## No temperature

None of the classes takes `temperature`, `top_p` or `top_k`. The current models either ignore them (Gemini 3.6), reject
them with a 400 (Claude 5), or ask you to omit them (GPT-6.2), and a setting that is silently dropped hides a mistake.
`Claude(temperature=0)` says so:

```
Claude has no `temperature`: the current models of this provider refuse or ignore it, ...
```

To make output more repeatable, give the agent clear rules in its prompt, use enums in the output model, and use
`check_output`. No provider documents a switch for it. A class for a model that does accept temperature can declare the field
([Ollama](#add-a-provider-ollama) below).

## Add a model: GPT-5.x

The built-in `OpenAI` class lists the GPT-6 models. To use GPT-5.5, subclass it and add the model to its catalog:

```python
from typing import ClassVar

from micro.model_settings import ModelInfo, OpenAI


class OpenAI5(OpenAI):
    models: ClassVar[dict[str, ModelInfo]] = {
        **OpenAI.models,
        "gpt-5.5": ModelInfo(efforts=("none", "low", "medium", "high"), default="medium"),
    }


class Reader(AgentBase):
    model: OpenAI5 = OpenAI5(model="gpt-5.5", effort="low")
```

`efforts` is what the model accepts. Everything else, including `build()`, is inherited, so the new model gets the same
validation: `OpenAI5(model="gpt-5.5", effort="max")` is an error, and so is `OpenAI(model="gpt-5.5")`, because the built-in
class is unchanged.

Check what the older model needs. GPT-5 models, for instance, refuse a temperature other than 1 unless the effort is `none`.
If you want a field, add it to the subclass and override `build()`.

## Add a provider: Ollama

Any provider that LangChain has a chat model for works. Write a subclass with its fields, and `build()`:

```python
from typing import ClassVar

from pydantic import Field

from micro.model_settings import ModelSettings


class Ollama(ModelSettings):
    provider: ClassVar[str] = "ollama"
    temperature: float | None = Field(default=None, ge=0, le=1)  # its own field, so it is accepted
    reasoning: bool | None = None

    def build(self):
        from langchain_ollama import ChatOllama  # imported here, so `micro` does not need it

        return ChatOllama(model=self.model, temperature=self.temperature, reasoning=self.reasoning)


class Local(AgentBase):
    model: ModelSettings = Ollama(model="gpt-oss:20b", temperature=0.2)
```

- **`build()` is required.** It is the only method you must write, and it returns the LangChain chat model. The base class
  does not provide one: if you leave it out, the class can still be created and put on an agent, but the first run fails
  with `NotImplementedError: <Class> must write build()`. A setup error is not retried, so the run fails at once. The runner
  calls `agent.model.build()` for every attempt.
- With no `models` catalog, any model name is accepted and efforts are not checked. Add a `models` catalog, and an
  `effort_field` naming the field that holds the effort, to get the checks of the built-in classes.
- `label()` is the short text the run log keeps. It is the effort field by default, and can be overridden.
- Raise `SetupError` (from `micro.errors`) for a problem that a retry cannot fix. `require("langchain_ollama", "ollama")`
  imports a package and raises one that names the install command.

Ollama's tool support depends on the model you pull, and its docs list token usage as unsupported, so check what your model
reports.

## Tokens and the run log

`input_tokens` and `output_tokens` on the result mean the same for every provider: all input, and all output **with
thinking**. The runner sums them over the model calls of the successful attempt.

The run log has one row for each attempt. For the model it keeps:

| Column | |
|---|---|
| `model_name`, `reasoning` | The model, and the effort or thinking level as text |
| `reported_model_name` | What the API reported |
| `provider` | `google`, `anthropic`, `openai`, or your class's `provider` |
| `settings` | All the settings as JSON, so a run can be reproduced |

Rows written before these columns have `NULL` in `provider` and `settings`.

## Limits

- **Gemma and tools.** With tools and an output model in one agent, both hosted Gemma models answered in JSON at once and
  did not call the tool, where Gemini did. Prefer Gemma for agents without tools.
- **Claude's bounds.** The API does not enforce numeric bounds in an output model (`ge`, `le`), so use `check_output`.
- **OpenAI.** Tested with `gpt-6-luna` only. Every OpenAI model here uses the Responses API.
- **Older models** (Gemini 2.x, Claude 4, GPT-5) are not built in. Add them as above.
- **Not available:** token budgets (`thinking_budget`, `budget_tokens`), `verbosity`, `reasoning.mode: pro`, a different
  model on a retry, and a cost report. A subclass can add the fields it needs.
