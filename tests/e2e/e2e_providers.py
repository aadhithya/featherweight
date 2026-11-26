"""The providers that the real-model tests run on, each with its smallest model. One set of tests runs on all of them:
a test asks for `reasoning` ("off" or "low"), and a provider says which settings that is."""

import os
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from featherweight import Claude, Gemini, ModelSettings, OpenAI

OUTPUT_CAP = 2000  # the most output tokens of one model call, thinking included
STEPS = 20  # `recursion_limit` of every agent: a runaway tool loop stops early


class Provider(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    keys: tuple[str, ...]  # one of these must be set
    settings: Callable[[str], ModelSettings]

    def has_key(self) -> bool:
        return any(os.environ.get(key) for key in self.keys)

    def on(self, agent: type[BaseModel], reasoning: str = "low", **fields) -> Any:
        """The same agent, on this provider. `fields` replaces more of its fields, for example its sub-agents."""
        model = self.settings(reasoning)
        replaced: dict[str, Any] = {"model": (ModelSettings, model), "recursion_limit": (int, STEPS)}
        replaced |= {name: (agent.model_fields[name].annotation, value) for name, value in fields.items()}
        return create_model(agent.__name__, __base__=agent, **replaced)


def _gemini(reasoning: str) -> ModelSettings:
    # Gemini cannot switch thinking off: `minimal` is nearly off
    thinking = "minimal" if reasoning == "off" else "low"
    return Gemini(model="gemini-3.5-flash-lite", thinking=thinking, max_output_tokens=OUTPUT_CAP)


def _claude(reasoning: str) -> ModelSettings:
    thinking = "off" if reasoning == "off" else "adaptive"
    return Claude(model="claude-haiku-5-5", effort="low", thinking=thinking, max_output_tokens=OUTPUT_CAP)


def _openai(reasoning: str) -> ModelSettings:
    return OpenAI(model="gpt-6-luna", effort="none" if reasoning == "off" else "low", max_output_tokens=OUTPUT_CAP)


PROVIDERS = (
    Provider(name="gemini", keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"), settings=_gemini),
    Provider(name="claude", keys=("ANTHROPIC_API_KEY",), settings=_claude),
    Provider(name="openai", keys=("OPENAI_API_KEY",), settings=_openai),
)
