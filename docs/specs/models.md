# Models and providers

Status: implemented. How to use it is in [models.md](../models.md). The code is [model_settings.py](../../src/featherweight/model_settings.py).

It was designed in [issue #2](https://github.com/aadhithya/featherweight/issues/2). It changes
[agents](agents.md) (the `model_name` and `reasoning` fields), [the runner](runner.md#configuration) (the model factory is removed)
and [jobs](jobs.md) (two columns).

- [Goal](#goal)
- [The model](#the-model)
- [What is in the catalog](#what-is-in-the-catalog)
- [Rules that hold for every provider](#rules-that-hold-for-every-provider)
- [Per provider](#per-provider)
- [Extending it](#extending-it)
- [Tokens and the run log](#tokens-and-the-run-log)
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
| `check_model_output_schema(output_model)` | Refuses an output model that this model cannot fill ([provider limits](#provider-limits)). The default accepts everything |

`ModelInfo` is frozen: `efforts` (the allowed values, in order), `default` (what the provider uses when none is sent,
for the docs), and for Claude `can_disable_thinking` and `native_output` ([provider limits](#provider-limits)).

### The model settings validate what an agent asks of its model

**The model settings are in charge of validating everything that depends on the provider or the model:** which model and
options an agent names, and which output it asks the model to fill. What a provider takes differs by provider and by
model, so the class that knows the model holds the rule, and the runner and `AgentBase` know no provider: `AgentBase` calls
the hook. A settings class that a user writes states its own provider's rules in the same place. This does not change who
validates data: the data of a run is validated against the agent's `input_model` and `output_model`, and by
`check_output`. Nor is a reply the settings' to judge: the runner refuses an [early answer](runner.md#an-early-answer),
whatever the model.

| What is validated | By | When |
|---|---|---|
| The model and its options | The setting's own validators | When the setting is built |
| The agent's output model | `check_model_output_schema` | When the agent is built |

**Why: Anthropic is the example, and the reason this was added** (2026-11-26). Claude Opus, Sonnet and Fable take only
some output schemas, and one of the others, a dictionary, is not refused by anyone: Anthropic's SDK empties it, and the run
succeeds without the data. The rule is not true of every provider, nor of every Claude model (Haiku takes those schemas),
so it belongs neither in the agent nor in the runner
([provider limits](#provider-limits)).

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
| Tools and the reply schema | Native | **Caveat below** | Opus, Sonnet, Fable: native. Haiku: the reply is a tool call ([limits](#provider-limits)). Forced `tool_choice` is rejected on Opus 5.5, Sonnet 5.5 and Fable, which LangChain does not use | Responses API; Chat Completions only at effort `none` on Luna |

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
or at `xhigh` or `max`. How the output is asked for differs by model, and so do the limits ([provider limits](#provider-limits)):
Opus, Sonnet and Fable get Anthropic's structured output, and Haiku gets a tool call. The API does not enforce a numeric
bound, so `check_output` is the guard.

## Provider limits

An output schema that a provider cannot fill is refused by **the model settings**, because that differs by provider and
by model. featherweight passes the output model to LangChain as it is: LangChain chooses how to ask for it, and the
provider's own SDK converts the schema. We add no converter and no middleware. We only refuse.

`ModelSettings.check_model_output_schema(output_model)` is called when an agent is built (not for a streaming agent: it
has no typed output), and by default accepts everything.

**Schemas that Claude's structured output does not take.** `Claude.check_model_output_schema` applies to the models whose
`ModelInfo.native_output` is true (Opus 5.5, Sonnet 5.5, Fable 5.2), which is where LangChain asks for Anthropic's structured
output. It raises `ValueError`, so the agent is not built, and the message names every problem:

| Output model has | Without the check |
|---|---|
| A free-form object: a `dict` of any kind, also in a list or optional, or a model with `extra="allow"` | Anthropic's SDK (`transform_schema`) sends an object with no properties that allows no others. **The field comes back empty, and the run succeeds** |
| A recursive model | The API refuses the request (400) |
| More than 24 optional fields, those of nested models and `open_questions` included | The API refuses the request (400) |

The optional fields are counted from the model's JSON schema, with a definition that is used twice counted once, so the count
is never higher than the provider's, and an output that the API takes is not refused for it. A model that a user adds to the
catalog says `native_output=True` itself. `Claude(model="claude-haiku-5-5")` is not held to these limits: LangChain asks it
for the output as a tool call, which sends the schema as it is. Types without a JSON schema type (`Any`, a bare `list`,
`object`) are not checked: the SDK raises for them before anything is sent.

**An early answer** is the other limit seen with Claude Haiku, which gets its output as a tool call: it can give the answer
before it has seen its tools' results. The runner refuses that, whatever the model ([an early answer](runner.md#an-early-answer)).

**When the answer does not validate.** With the output as a tool call, LangChain sends a validation error back to the model
within the attempt. With a provider's structured output (Claude's native models, and OpenAI), a reply that does not validate
fails the attempt.

**Decided (2026-11-26).** Refuse, and document. Rejected:

- **Middleware that sends an early answer back to the model.** It works in a prototype, with one call more, but it makes
  featherweight own the agent loop's edge cases, one middleware after another.
- **A schema converter of our own.** The conversion is the provider's, and a second one would drift from it.
- **Asking Haiku for native output** (by changing what LangChain knows about it). In an offline prototype it removes the
  early answer, and brings all three schema limits to the default Claude model. It was not run against the real API.
- **Only documenting the dictionary.** The data is lost while the run succeeds, so a warning that is missed costs data.

Seen on the real API on 2026-11-26: Haiku 5.5, with thinking on, gave an empty answer in one reply with a tool call
(inferred: one model call, the tool ran, every field empty; the reply itself was not kept, and the case was reproduced with a
fake). Sonnet 5.5 returned `{}` for a `dict[str, str]` it was asked to fill, and refused a recursive schema and one with 31
optional fields. Opus 5.5 and Fable 5.2 were not run: LangChain asks them the same way.

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

**The run log** records the requested model, the effort (the `label()`), the provider (`google`, `anthropic`, `openai`, or the class's own) and
the settings as JSON, so that a run can be reproduced. The columns are in [the schema](persistence.md#the-schema).

## Testing

A fake is model settings. A test defines a `ModelSettings` subclass whose `build()` returns a scripted chat model, and runs
the agent with its `model` replaced by that settings object. The runner calls `agent.model.build()` as in production, and
nothing in the runner exists for tests. The suite does this with `FakeSettings` in `tests/fakes.py`, which looks up the
scripted model a test registered under a key, so that an agent can be declared once and a test can choose its model. The
[guide](../models.md#testing-your-agents) and the agent skill show the pattern for users, with the fake chat model held by
the settings.

## Decisions

- **What depends on the model is validated by its settings** ([above](#the-model-settings-validate-what-an-agent-asks-of-its-model)),
  not by the runner or by `AgentBase`, which would need to know each provider and each model (2026-11-26).
- **One class per provider, with its own fields.** The controls differ in kind, so a shared `reasoning: Literal[...]` would
  be wrong for one of them. The label in the log is what is shared.
- **A catalog per class, checked when the setting is built.** A typo, or an effort a model refuses, fails at definition time.
  The cost is a change in featherweight when a model ships, and a user class is the way round it.
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
- Checking an output schema against a provider's whole schema subset. Only what is in [provider limits](#provider-limits) is checked.
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
migration, and an agent with a Claude and one with a Gemini setting. [Provider limits](#provider-limits): each refused
schema, the boundary of the optional fields, the models and providers that are not held to them, the early answer and its
retry ([the runner](runner.md#an-early-answer)), and two facts of the dependencies that the checks rest on (which Claude models LangChain asks for native output,
and that Anthropic's SDK still empties a dictionary). One real-model test per provider, `claude-haiku-5-5`,
`gpt-6-luna` and a small Gemma, run only on request.
