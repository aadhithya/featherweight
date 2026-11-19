# Spec: models and providers

Status: implemented. How to use it is in [models.md](../models.md). The code is [model_settings.py](../../src/featherweight/model_settings.py).

It was designed in [issue #2](https://github.com/aadhithya/featherweight/issues/2). It changes
[agents](agents.md) (the `model_name` and `reasoning` fields), [the runner](runner.md#configuration) (the model factory is removed)
and [jobs and tracing](jobs-and-tracing.md) (two columns).

- [Goal](#goal)
- [The model](#the-model)
- [What is in the catalog](#what-is-in-the-catalog)
- [Rules that hold for every provider](#rules-that-hold-for-every-provider)
- [Per provider](#per-provider)
- [Extending it](#extending-it)
- [Tokens and the run log](#tokens-and-the-run-log)
- [What changes](#what-changes)
- [Testing](#testing)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Findings](#findings)
- [Tests](#tests)

## Goal

An agent says which model runs it, and how hard it thinks, with a typed object that checks itself. Each provider has its
own class with its own fields, because the providers do not share their controls: a thinking level, an effort, an on/off
switch. A setting that a model does not accept fails **when the agent is defined**, not on the first call. Anyone can add a
provider, or a model, by writing a class.

## The model

`ModelSettings` is a frozen Pydantic model that rejects unknown fields (`extra="forbid"`). An agent has one:

```python
class Triager(AgentBase):
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low")
```

| Member | |
|---|---|
| `provider` | A class attribute: `"google"`, `"anthropic"`, `"openai"`. Stored in the run log |
| `models` | A class attribute: the catalog of the class, model id to `ModelInfo` |
| `model` | The model id. Must be in `models` |
| `max_output_tokens` | Optional. The most tokens the model may produce, **thinking included** |
| `show_thoughts` | `False` by default. Asks the provider to return the model's thoughts (see [streaming agents](streaming.md#the-models-thoughts)). Gemini and Gemma send `include_thoughts`, Claude `thinking.display = "summarized"`, OpenAI `reasoning.summary = "auto"`. Refused for `Claude(thinking="off")` and `OpenAI(effort="none")` |
| `build()` | Returns the LangChain chat model. Imports the provider's package when called. **Required: the only method a new class must write.** The base class raises `NotImplementedError("<Class> must write build()")`, when the model is built for a run, not when the class is defined |
| `label()` | A short text for the log, such as `medium`, `off` or `high`. By default the effort or thinking field |

`ModelInfo` is frozen: `efforts` (the allowed values, in order), `default` (what the provider uses when none is sent,
for the docs), and for Claude `can_disable_thinking`.

**Validation, when the setting is built:**

- `model` must be in the catalog, and the effort (or thinking level) must be one of that model's `efforts`. The message
  lists what is allowed.
- A field the class does not declare is an error. In particular `temperature`, `top_p` and `top_k` are not fields of any
  built-in class, and passing one raises a message that says why ([below](#temperature)).

`AgentBase.model` replaces `model_name` and `reasoning`. The default is `Gemini()`.

## What is in the catalog

| Class | Models | Controls |
|---|---|---|
| `Gemini` | `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite` | `thinking`: `minimal`, `low`, `medium`, `high`, as each model allows |
| `Gemma` | `gemma-4-31b-it`, `gemma-4-26b-a4b-it` | `thinking`: `minimal` (off) or `high` |
| `Claude` | `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-2` | `effort`: `low`, `medium`, `high`, `xhigh`, `max`. `thinking`: `adaptive` or `off` |
| `OpenAI` | `gpt-6.2-astra`, `gpt-6.2-sol`, `gpt-6.1-sol`, `gpt-6.1-terra`, `gpt-6-astra`, `gpt-6-luna` | `effort`: `none`, `low`, `medium`, `high`, `xhigh`, `max`, as each model allows |

Only Gemini 3 and later, Gemma 4, Claude 5 and later, and GPT-6 are built in. Older models are for a user class
([Extending it](#extending-it)). The exact efforts of each model are in the code and the [table below](#per-provider).

## Rules that hold for every provider

### Temperature

No built-in class has `temperature`, `top_p` or `top_k`.

- Gemini 3.6: "deprecated and ignored", and a 400 in later generations. The advice is a system instruction with explicit rules.
- Claude 5: a non-default value is a **400**, and so is `top_p=1` (its default is 0.99) and sending `temperature` and `top_p` together.
  From API version 2026-10-21 any value is a 400.
- GPT-6.2: the migration guide says to omit `temperature` and `top_p`.
- `langchain-openai` drops a non-1 temperature silently on GPT-5 models, and `langchain-google-genai` sets it to `None` on Gemini 3.
  A silent drop hides a mistake, so these classes refuse it.

A class whose models accept temperature declares it itself (the [Ollama example](#a-provider-ollama)). To make an agent
more repeatable, use the prompt, an output schema with enums, and `check_output`. No provider documents a switch for
repeatable output.

### Thinking is counted in tokens

`max_output_tokens` includes thinking on all three providers. For Claude it is **required by the API**, so `Claude` has a
default of 16,000 and the docs advise 64,000 or more at `xhigh` and `max`.

### Setup errors

A provider package that is not installed, a missing key, and an invalid setting at build time are a `ModelSetupError`:
not retried, as before. A package that is not installed says which extra to install (`pip install 'featherweight[anthropic]'`).

## Per provider

| | Gemini | Gemma | Claude | OpenAI |
|---|---|---|---|---|
| Package | `langchain-google-genai` (always installed) | same | `langchain-anthropic`, extra `anthropic` | `langchain-openai`, extra `openai` |
| Key | `GOOGLE_API_KEY` | same | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` |
| Sent as | `thinking_level` | `thinking_level` | `reasoning_effort`, and `thinking={"type": "adaptive"}` or `{"type": "disabled"}` | Responses API, `reasoning={"effort": ...}` |
| Can thinking be off? | No (`minimal` is nearly off) | `minimal` | Opus 5.5, Sonnet 5.5, Fable: no. Haiku 5.5: yes, at `high` or below | `none`, on `gpt-6.1-terra` and `gpt-6-luna` only |
| Tools and the reply schema | Native | **Caveat below** | Native; forced `tool_choice` is rejected on Opus 5.5, Sonnet 5.5 and Fable, which LangChain does not use | Responses API; Chat Completions only at effort `none` on Luna |

Efforts per model:

| Model | Efforts | Default |
|---|---|---|
| `gemini-3.8-flash` | low, medium, high | medium |
| `gemini-3.6-flash` | minimal, low, medium, high | medium |
| `gemini-3.5-flash-lite` | minimal, low, medium, high | minimal |
| `gemma-4-31b-it`, `gemma-4-26b-a4b-it` | minimal, high | |
| `claude-opus-5-5`, `claude-haiku-5-5` | low, medium, high, xhigh, max | medium |
| `claude-sonnet-5-5`, `claude-fable-5-2` | low, medium, high, xhigh, max | high |
| `gpt-6.2-astra`, `gpt-6.2-sol`, `gpt-6.1-sol`, `gpt-6-astra` | low, medium, high, xhigh, max | medium (not stated for the 6.2 models; `medium` is the docs' advice) |
| `gpt-6.1-terra`, `gpt-6-luna` | none, low, medium, high, xhigh, max | medium |

**The class default is not always the provider's default.** `Gemini`, `Claude` and `OpenAI` all default `effort` or
`thinking` to `medium` (`Gemma` to `minimal`), so that a bare `Claude(model=...)` behaves alike across providers. The
table says what the provider would use.

**Gemma.** It takes `thinking_level` through `ChatGoogleGenerativeAI`, and reports thinking tokens. With a tool and a reply
schema in one agent, both hosted models answered in JSON at once and did not call the tool, where Gemini did. Gemma is
supported, and this is documented as a limit: **prefer Gemma for agents without tools**, until it is tested further. Hosted
Gemma has image input, no audio, and a 256K context.

**OpenAI.** `OpenAI.build()` always uses the Responses API, because tool calling needs it on the models above. It is tested
with `gpt-6-luna` at `none` and `low`: a tool call and our reply schema, with a defaulted field and a bounded integer, both
worked.

**Claude.** `Claude(thinking="off")` sends `{"type": "disabled"}`, and is refused for a model that cannot ([info](#the-model))
or at `xhigh` or `max`. Tested with `claude-haiku-5-5` at `low`, `claude-sonnet-5-5` and `claude-opus-5-5`: native structured
output, a defaulted field and a bounded integer worked. The API does not enforce the bound, so `check_output` is the guard.

## Extending it

### A model of a provider that is built in

Subclass the class and add to its catalog. For example, GPT-5.x:

```python
from typing import ClassVar

from featherweight import ModelInfo, OpenAI


class OpenAI5(OpenAI):
    models: ClassVar[dict[str, ModelInfo]] = {
        **OpenAI.models,
        "gpt-5.5": ModelInfo(efforts=("none", "low", "medium", "high"), default="medium"),
    }


class Reader(AgentBase):
    model: OpenAI5 = OpenAI5(model="gpt-5.5", effort="low")
```

`build()` is inherited. Rules of the older models (such as GPT-5's refusal of a temperature) are the user's to add, by
overriding `build()` or adding a field.

### A provider: Ollama

Write `build()`, name the fields:

```python
class Ollama(ModelSettings):
    provider: ClassVar[str] = "ollama"
    models: ClassVar[dict[str, ModelInfo]] = {}  # empty: any model name the server has
    temperature: float | None = Field(default=None, ge=0, le=1)
    reasoning: bool | None = None

    def build(self):
        from langchain_ollama import ChatOllama

        return ChatOllama(model=self.model, temperature=self.temperature, reasoning=self.reasoning)
```

An **empty catalog means any model id is allowed**, and efforts are not checked. Nothing else is needed: the runner calls
`agent.model.build()`, and the log stores `provider` and the settings.

`label()` can be overridden when the class has no `effort` or `thinking` field.

## Tokens and the run log

**Tokens.** The runner sums `usage_metadata["input_tokens"]` and `["output_tokens"]` of the model's replies. This was
checked in the source of each LangChain package, and with live calls:

| | `input_tokens` | `output_tokens` |
|---|---|---|
| Gemini, Gemma | Prompt tokens (cached included) plus tool-use prompt tokens | Reply tokens **plus thinking tokens**, added by the package |
| Claude | Input plus cache reads plus cache writes, added back by the package | As the API reports, thinking included |
| OpenAI | Prompt tokens, cached included | Completion tokens, reasoning included |

All three give `input_tokens` as the whole input and `output_tokens` as the whole output with thinking, so
[`_usage`](../../src/featherweight/runner.py) needs no change. The reported model name is `response_metadata["model_name"]` for
Gemini, Gemma, Claude and OpenAI. A class that does not set it (Ollama puts `model`) leaves `AgentResult.model_name` empty.
Cache reads and writes are available in `input_token_details` and are not stored.

**The run log** keeps `model_name` (the requested model) and `reasoning` (the `label()`), and adds:

| Column | |
|---|---|
| `provider` | `google`, `anthropic`, `openai`, or the class's own |
| `settings` | The settings as JSON (`model_dump_json()`), so the run can be reproduced |

Both are added by the migration that adds columns to an older log. They are `NULL` for rows written before. The tracing
spec and the [guide](../agents.md) are updated.

## What changes

- `AgentBase`: `model_name` and `reasoning` are removed, `model` is added. **This breaks every agent.** The library is not
  released, and the examples, docs and tests change with it.
- `ModelName` and `Reasoning` are removed. `ModelSettings`, `ModelInfo`, `Gemini`, `Gemma`, `Claude` and `OpenAI` are
  in `featherweight.model_settings`.
- `default_model_factory(agent)` becomes `agent.model.build()`, and the `model_factory` argument is removed
  ([testing](#testing)). The agent's settings are the only way to choose a model.
- `pyproject.toml`: extras `anthropic` and `openai`, and both in the dev group.
- `.env.example` has the three keys.

## Testing

A fake is model settings. A test defines a `ModelSettings` subclass whose `build()` returns a scripted chat model, and runs
the agent with its `model` replaced by that settings object. The runner calls `agent.model.build()` as in production, and
nothing in the runner exists for tests. The suite does this with `FakeSettings` in `tests/fakes.py`, which looks up the
scripted model a test registered under a key, so that an agent can be declared once and a test can choose its model. The
[guide](../models.md#testing-your-agents) and the agent skill show the pattern for users, with the fake chat model held by
the settings.

## Decisions

- **One class per provider, with its own fields.** The controls differ in kind, so a shared `reasoning: Literal[...]` would
  be wrong for one of them. The label in the log is what is shared.
- **A catalog per class, checked when the setting is built.** A typo, or an effort a model refuses, fails at definition time.
  The cost is a change in Featherweight when a model ships, and a user class is the way round it.
- **No hook on the runner.** A second way to choose the model, used only by tests, would let a run use a model its
  declaration does not name, and would leave the declaration unable to say what ran. Tests use fake settings instead.
- **No registry.** The class is the extension point and the runner calls `build()`. Nothing is registered.
- **No temperature.** The vendors have removed or discourage it, and silently ignoring it is worse than refusing it.
- **Only current families.** Older models need other parameters (`budget_tokens`, `thinking_budget`), and a user class can
  add them.
- **Settings are stored as JSON in the log.** A run can be reproduced, and a new field needs no new column.
- **Gemma is in, with a limit.** The limit is in the docs and not hidden.

## Not supported

- Older models (Gemini 2.x, Claude 4 and earlier, GPT-5) in core. Add them as in [Extending it](#extending-it).
- Token budgets (`thinking_budget`, `budget_tokens`), the OpenAI `verbosity` setting and `reasoning.mode: pro`, and
  Claude's `task_budget`. A user class can add each.
- Choosing a model by a string alone (`model="claude-sonnet-5-5"`).
- Different settings per attempt (a stronger model on a retry).
- Checking the output schema against a provider's schema subset. All three accepted our schemas in the tests.
- Cost, or storing cached-token counts.
- Provider-side features: built-in tools, web search, prompt caching.

## Findings

What the vendors say, and what was run (2026-11-18).

| | |
|---|---|
| Claude, live (haiku-5-5, sonnet-5-5, opus-5-5, low effort) | Tool call, then reply as JSON: native structured output. Defaulted field and a bounded integer worked. Usage and model name reported |
| OpenAI, live (`gpt-6-luna`: Chat Completions `none`, Responses `none` and `low`) | The same, three times. Identical token counts in all three, and 0 reasoning tokens even at `low`, so it is not shown that the effort reached the API. Strict mode is not shown to be on |
| Gemma, live (both hosted models) | Reply schema, usage and model name work. Tool calling works when the tool is bound alone. With a reply schema in the same agent, the tool was skipped |
| Ollama (docs only) | `reasoning` is `True`, `False` or `None`; `temperature` 0 to 1; the default structured-output method is `json_schema`; the docs list token usage as unsupported and show counts in one example |

Sources: the vendors' documentation for [Gemini thinking](https://aistudio.google.com/docs/thinking), [Claude
effort](https://platform.claude.com/docs/en/build-with-claude/effort), [Claude
thinking](https://platform.claude.com/docs/en/build-with-claude/thinking), [Claude structured
outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [OpenAI
reasoning](https://developers.openai.com/api/docs/guides/reasoning) and [OpenAI structured
outputs](https://developers.openai.com/api/docs/guides/structured-outputs), and the installed source of the three
LangChain packages. Parts of the vendor pages were read through a summary, not verbatim.

## Tests

Offline, and with no key. For each class: the catalog (a model not in it, an effort the model refuses, a field it does not
have, `temperature` with its message), `label()`, and `build()` with a fake key, which checks what the LangChain object
holds (`reasoning_effort`, `thinking`, `max_tokens`, `use_responses_api`, `reasoning`, `thinking_level`). A missing package is a
`ModelSetupError` that names the extra. A user class (GPT-5 and Ollama as in the examples above), the log columns and the
migration, and an agent with a Claude and one with a Gemini setting. One real-model test per provider, `claude-haiku-5-5`,
`gpt-6-luna` and a small Gemma, run only on request.
