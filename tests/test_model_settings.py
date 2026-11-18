"""The model settings: what each class accepts, what it builds, and how a user class extends them."""

import sys
from typing import ClassVar

import pytest
from pydantic import Field, ValidationError

from micro.errors import SetupError
from micro.model_settings import Claude, Gemini, Gemma, ModelInfo, ModelSettings, OpenAI

PROVIDERS = [Gemini, Gemma, Claude, OpenAI]


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for name in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "fake-key")


@pytest.mark.parametrize("cls", PROVIDERS)
def test_every_class_builds_with_its_defaults_and_names_its_provider(cls):
    settings = cls()
    assert settings.model in cls.models
    assert settings.provider in {"google", "anthropic", "openai"}
    assert settings.build() is not None


@pytest.mark.parametrize("cls", PROVIDERS)
def test_a_model_outside_the_catalog_is_refused_and_the_message_lists_the_models(cls):
    with pytest.raises(ValidationError, match="is not a model of") as error:
        cls(model="gpt-4o")
    assert next(iter(cls.models)) in str(error.value)


@pytest.mark.parametrize("cls", PROVIDERS)
@pytest.mark.parametrize("name", ["temperature", "top_p", "top_k"])
def test_sampling_settings_are_refused_with_a_reason(cls, name):
    with pytest.raises(ValidationError, match=f"has no `{name}`"):
        cls(**{name: 0})


def test_an_unknown_field_is_refused():
    with pytest.raises(ValidationError):
        Gemini.model_validate({"thinking_budget": 100})


def test_settings_are_frozen():
    with pytest.raises(ValidationError):
        Gemini().thinking = "high"


def test_max_output_tokens_must_be_positive():
    with pytest.raises(ValidationError):
        Gemini(max_output_tokens=0)


# --- Gemini and Gemma


def test_gemini_efforts_differ_by_model():
    Gemini(model="gemini-3.6-flash", thinking="minimal")
    with pytest.raises(ValidationError, match="does not accept thinking='minimal'"):
        Gemini(model="gemini-3.8-flash", thinking="minimal")
    with pytest.raises(ValidationError):
        Gemini(thinking="extreme")


def test_gemini_sends_the_thinking_level_and_the_token_limit():
    chat = Gemini(model="gemini-3.6-flash", thinking="low", max_output_tokens=500).build()
    assert chat.model == "gemini-3.6-flash"
    assert chat.thinking_level == "low" and chat.max_output_tokens == 500


def test_gemma_thinks_minimal_or_high_and_sends_it_the_same_way():
    chat = Gemma(model="gemma-4-31b-it", thinking="high").build()
    assert chat.thinking_level == "high"
    with pytest.raises(ValidationError):
        Gemma(thinking="medium")


# --- Claude


def test_claude_sends_effort_and_adaptive_thinking_and_always_a_token_limit():
    chat = Claude(model="claude-sonnet-5-5", effort="xhigh").build()
    assert chat.effort == "xhigh"
    assert chat.thinking == {"type": "adaptive"}
    assert chat.max_tokens == 16_000


def test_claude_can_turn_thinking_off_only_where_the_model_allows_it():
    chat = Claude(model="claude-haiku-5-5", effort="low", thinking="off").build()
    assert chat.thinking == {"type": "disabled"}
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"):
        with pytest.raises(ValidationError, match="cannot turn thinking off"):
            Claude(model=model, thinking="off")


def test_claude_thinking_is_off_only_at_high_effort_or_below():
    with pytest.raises(ValidationError, match="can only be off at"):
        Claude(model="claude-haiku-5-5", effort="xhigh", thinking="off")


def test_claudes_label_is_off_or_the_effort():
    assert Claude(effort="max").label() == "max"
    assert Claude(model="claude-haiku-5-5", thinking="off").label() == "off"


# --- OpenAI


def test_openai_uses_the_responses_api_with_the_effort_in_reasoning():
    chat = OpenAI(model="gpt-6.2-sol", effort="high", max_output_tokens=900).build()
    assert chat.use_responses_api is True
    assert chat.reasoning == {"effort": "high"}
    assert chat.max_tokens == 900


def test_openai_none_effort_is_for_the_models_that_accept_it():
    OpenAI(model="gpt-6-luna", effort="none")
    for model in ("gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-astra"):
        with pytest.raises(ValidationError, match="does not accept effort='none'"):
            OpenAI(model=model, effort="none")


def test_label_is_the_effort():
    assert Gemini(thinking="high").label() == "high"
    assert OpenAI(effort="low").label() == "low"


# --- Missing packages


@pytest.mark.parametrize(
    "cls,module,extra",
    [(Claude, "langchain_anthropic", "anthropic"), (OpenAI, "langchain_openai", "openai")],
)
def test_a_missing_provider_package_is_a_setup_error_that_names_the_extra(monkeypatch, cls, module, extra):
    monkeypatch.setitem(sys.modules, module, None)  # makes the import fail
    with pytest.raises(SetupError, match=f"micro\\[{extra}\\]"):
        cls().build()


# --- Extending it


def test_gpt5_is_added_by_extending_the_catalog():
    class OpenAI5(OpenAI):
        models: ClassVar[dict[str, ModelInfo]] = {
            **OpenAI.models,
            "gpt-5.5": ModelInfo(efforts=("none", "low", "medium", "high"), default="medium"),
        }

    assert OpenAI5(model="gpt-5.5", effort="low").build().reasoning == {"effort": "low"}
    with pytest.raises(ValidationError, match="does not accept effort='max'"):
        OpenAI5(model="gpt-5.5", effort="max")
    with pytest.raises(ValidationError):
        OpenAI(model="gpt-5.5")  # the built-in class is unchanged


def test_a_new_provider_needs_only_build():
    class Ollama(ModelSettings):
        provider: ClassVar[str] = "ollama"
        temperature: float | None = Field(default=None, ge=0, le=1)  # its own field, so it is accepted
        reasoning: bool | None = None

        def build(self):
            return ("ollama", self.model, self.temperature, self.reasoning)

    settings = Ollama(model="llama3.2", temperature=0.2)  # an empty catalog accepts any model id
    assert settings.build() == ("ollama", "llama3.2", 0.2, None)
    assert settings.label() == ""
    with pytest.raises(ValidationError):
        Ollama(model="llama3.2", temperature=2)
    with pytest.raises(ValidationError, match="has no `top_p`"):
        Ollama.model_validate({"model": "llama3.2", "top_p": 0.5})


def test_a_class_that_does_not_write_build_says_so():
    class Bare(ModelSettings):
        provider: ClassVar[str] = "bare"

    with pytest.raises(NotImplementedError, match="must write build"):
        Bare(model="x").build()
