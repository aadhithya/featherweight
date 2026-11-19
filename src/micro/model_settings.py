"""Which model runs an agent, and how hard it thinks: one typed class per provider.

Each class holds what that provider's models accept, checks it when it is built, and builds the LangChain chat model.
To add a provider, or a model, write a subclass (see docs/models.md); the runner only calls `build()`.
"""

import importlib
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from micro.errors import SetupError

# Sampling settings that the current models refuse or ignore. No built-in class has them (see docs/specs/models.md).
SAMPLING_FIELDS = ("temperature", "top_p", "top_k")
SAMPLING_ADVICE = (
    "the current models of this provider refuse or ignore it, and a silent drop would hide the mistake. "
    "Use the prompt, an output schema with enums, and `check_output` to make the output more repeatable."
)


class ModelInfo(BaseModel):
    """What one model accepts."""

    model_config = ConfigDict(frozen=True)

    efforts: tuple[str, ...]  # the values of the class's effort (or thinking) field that this model accepts
    default: str | None = None  # what the provider uses when none is sent; for the docs, not used
    can_disable_thinking: bool = False  # Claude: whether `thinking="off"` is accepted


def require(module: str, extra: str) -> Any:
    """Import a provider package when a model is built, and say which extra installs it when it is missing."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise SetupError(f"{module} is not installed: pip install 'micro[{extra}]'") from exc


class ModelSettings(BaseModel):
    """Base of the model settings of an agent. A subclass names the provider, lists its models, and writes `build()`.

    Frozen, and unknown fields are an error: a setting the class does not declare is one the provider does not take.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ClassVar[str]
    models: ClassVar[dict[str, ModelInfo]] = {}  # empty: any model id is accepted, and efforts are not checked
    effort_field: ClassVar[str | None] = None  # the field the log label and the effort check read

    model: str
    max_output_tokens: int | None = Field(default=None, ge=1)  # includes thinking tokens
    # Ask the provider to return the model's thoughts (a summary, for most providers), so that a streaming agent can show
    # them. Off by default. How hard the model thinks is a separate setting.
    show_thoughts: bool = False

    @model_validator(mode="before")
    @classmethod
    def _refuse_sampling(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for name in SAMPLING_FIELDS:
                if name in data and name not in cls.model_fields:
                    raise ValueError(f"{cls.__name__} has no `{name}`: {SAMPLING_ADVICE}")
        return data

    @model_validator(mode="after")
    def _check_catalog(self) -> "ModelSettings":
        if not self.models:
            return self
        info = self.models.get(self.model)
        if info is None:
            raise ValueError(
                f"{self.model!r} is not a model of {type(self).__name__}. Its models: {', '.join(self.models)}"
            )
        if self.effort_field is not None:
            value = getattr(self, self.effort_field)
            if value not in info.efforts:
                raise ValueError(
                    f"{self.model} does not accept {self.effort_field}={value!r}. It accepts: {', '.join(info.efforts)}"
                )
        return self

    def label(self) -> str:
        """A short text for the run log: the effort or thinking level."""
        return str(getattr(self, self.effort_field)) if self.effort_field else ""

    def build(self):
        """The LangChain chat model. Imports the provider's package here, and raises `SetupError` when it is missing."""
        raise NotImplementedError(f"{type(self).__name__} must write build()")


class _Google(ModelSettings):
    """What Gemini and Gemma share: both go through `ChatGoogleGenerativeAI` with a `thinking_level`."""

    provider: ClassVar[str] = "google"
    effort_field: ClassVar[str | None] = "thinking"

    thinking: str

    def build(self):
        chat = require("langchain_google_genai", "google").ChatGoogleGenerativeAI
        extra: dict[str, Any] = {"max_output_tokens": self.max_output_tokens} if self.max_output_tokens else {}
        if self.show_thoughts:
            extra["include_thoughts"] = True
        return chat(model=self.model, thinking_level=self.thinking, **extra)


class Gemini(_Google):
    """Gemini 3 and later. Thinking cannot be switched off; `minimal` is nearly off. No temperature: the model ignores it."""

    models: ClassVar[dict[str, ModelInfo]] = {
        "gemini-3.8-flash": ModelInfo(efforts=("low", "medium", "high"), default="medium"),
        "gemini-3.6-flash": ModelInfo(efforts=("minimal", "low", "medium", "high"), default="medium"),
        "gemini-3.5-flash-lite": ModelInfo(efforts=("minimal", "low", "medium", "high"), default="minimal"),
    }

    model: str = "gemini-3.5-flash-lite"
    thinking: Literal["minimal", "low", "medium", "high"] = "medium"


class Gemma(_Google):
    """The Gemma 4 models the Gemini API hosts. Thinking is `minimal` (off) or `high`.

    With tools and a reply schema in one agent, both models answered at once and skipped the tool where Gemini called
    it. Prefer Gemma for agents without tools.
    """

    models: ClassVar[dict[str, ModelInfo]] = {
        "gemma-4-31b-it": ModelInfo(efforts=("minimal", "high")),
        "gemma-4-26b-a4b-it": ModelInfo(efforts=("minimal", "high")),
    }

    model: str = "gemma-4-26b-a4b-it"
    thinking: Literal["minimal", "high"] = "minimal"


_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_OFF_EFFORTS = ("low", "medium", "high")  # thinking can only be off at these


class Claude(ModelSettings):
    """Claude 5 and later. `effort` sets how hard the model thinks, and `thinking="off"` is for the models that allow it.

    The API needs `max_tokens`, and thinking counts toward it, so it defaults to 16,000. Raise it at `xhigh` and `max`.
    A non-default `temperature`, `top_p` or `top_k` is a 400 on these models, so there are no such fields.
    """

    provider: ClassVar[str] = "anthropic"
    effort_field: ClassVar[str | None] = "effort"
    models: ClassVar[dict[str, ModelInfo]] = {
        "claude-opus-5-5": ModelInfo(efforts=_CLAUDE_EFFORTS, default="medium"),
        "claude-sonnet-5-5": ModelInfo(efforts=_CLAUDE_EFFORTS, default="high"),
        "claude-haiku-5-5": ModelInfo(efforts=_CLAUDE_EFFORTS, default="medium", can_disable_thinking=True),
        "claude-fable-5-2": ModelInfo(efforts=_CLAUDE_EFFORTS, default="high"),
    }

    model: str = "claude-haiku-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    thinking: Literal["adaptive", "off"] = "adaptive"
    max_output_tokens: int = Field(default=16_000, ge=1)

    @model_validator(mode="after")
    def _check_thinking(self) -> "Claude":
        if self.thinking == "off" and self.show_thoughts:
            raise ValueError("show_thoughts needs thinking: it is off")
        if self.thinking == "off":
            info = self.models.get(self.model)
            if info is not None and not info.can_disable_thinking:
                raise ValueError(f"{self.model} cannot turn thinking off")
            if self.effort not in _CLAUDE_OFF_EFFORTS:
                raise ValueError(f"thinking can only be off at {', '.join(_CLAUDE_OFF_EFFORTS)}, not {self.effort!r}")
        return self

    def label(self) -> str:
        return "off" if self.thinking == "off" else self.effort

    def build(self):
        chat = require("langchain_anthropic", "anthropic").ChatAnthropic
        thinking: dict[str, str] = {"type": "disabled"} if self.thinking == "off" else {"type": "adaptive"}
        if self.show_thoughts:
            thinking["display"] = "summarized"  # newer models return thinking blocks with no text unless asked
        return chat(
            model=self.model, reasoning_effort=self.effort, thinking=thinking, max_tokens=self.max_output_tokens
        )


_GPT6_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class OpenAI(ModelSettings):
    """GPT-6 models, through the Responses API (tool calling needs it). `effort` is `reasoning.effort`.

    No `temperature` or `top_p`: the GPT-6.2 guide says to omit them. To add GPT-5.x, subclass and extend `models`.
    """

    provider: ClassVar[str] = "openai"
    effort_field: ClassVar[str | None] = "effort"
    models: ClassVar[dict[str, ModelInfo]] = {
        "gpt-6.2-astra": ModelInfo(efforts=_GPT6_EFFORTS, default="medium"),
        "gpt-6.2-sol": ModelInfo(efforts=_GPT6_EFFORTS, default="medium"),
        "gpt-6.1-sol": ModelInfo(efforts=_GPT6_EFFORTS, default="medium"),
        "gpt-6.1-terra": ModelInfo(efforts=("none", *_GPT6_EFFORTS), default="medium"),
        "gpt-6-astra": ModelInfo(efforts=_GPT6_EFFORTS, default="medium"),
        "gpt-6-luna": ModelInfo(efforts=("none", *_GPT6_EFFORTS), default="medium"),
    }

    model: str = "gpt-6-luna"
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "medium"

    @model_validator(mode="after")
    def _check_thoughts(self) -> "OpenAI":
        if self.show_thoughts and self.effort == "none":
            raise ValueError("show_thoughts needs reasoning: effort is 'none'")
        return self

    def build(self):
        chat = require("langchain_openai", "openai").ChatOpenAI
        extra = {"max_tokens": self.max_output_tokens} if self.max_output_tokens else {}
        reasoning = {"effort": self.effort}
        if self.show_thoughts:
            reasoning["summary"] = "auto"  # the raw reasoning is hidden; a summary of it is what the API returns
        return chat(model=self.model, use_responses_api=True, reasoning=reasoning, **extra)
