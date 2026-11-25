import pytest
from fakes import Echo, EchoOutput
from pydantic import ValidationError

from featherweight.mcp import HttpMCPServer, StdioMCPServer
from featherweight.model_settings import Claude, Gemini, Gemma, ModelSettings, OpenAI
from featherweight.models import (
    ASK_ALLOWED,
    ASK_FORBIDDEN,
    CONFLICTS_ALLOWED,
    CONFLICTS_FORBIDDEN,
    AgentBase,
    AgentResult,
    Status,
)
from featherweight.skills import Skill


class Plain(AgentBase):
    """An agent that keeps every default, the model settings included (the test agents run on fake settings)."""

    name: str = "plain"
    description: str = "Keeps the defaults"
    version: str = "0.1.0"
    prompt: str = "Do nothing."


def test_defaults():
    agent = Plain()
    assert agent.model == Gemini()
    assert (agent.model.model, agent.model.thinking) == ("gemini-3.5-flash-lite", "medium")
    assert agent.allow_user_questions is False
    assert agent.raise_conflicts is False


def test_an_agent_takes_the_settings_of_any_provider():
    class Reader(Echo):
        model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low")

    assert Reader().model.provider == "anthropic"


@pytest.mark.parametrize(
    "ask,conflicts,expected_ask,expected_conflicts",
    [
        (False, False, ASK_FORBIDDEN, CONFLICTS_FORBIDDEN),
        (True, False, ASK_ALLOWED, CONFLICTS_FORBIDDEN),
        (False, True, ASK_FORBIDDEN, CONFLICTS_ALLOWED),
        (True, True, ASK_ALLOWED, CONFLICTS_ALLOWED),
    ],
)
def test_system_prompt_includes_flag_instructions(ask, conflicts, expected_ask, expected_conflicts):
    agent = Echo(allow_user_questions=ask, raise_conflicts=conflicts)
    assert agent.build_system_prompt() == "\n\n".join([agent.prompt, expected_ask, expected_conflicts])


def test_system_prompt_hash_tracks_the_assembled_prompt():
    base = Echo()
    assert len(base.system_prompt_hash) == 64
    assert base.system_prompt_hash == Echo().system_prompt_hash
    assert Echo(allow_user_questions=True).system_prompt_hash != base.system_prompt_hash
    assert Echo(prompt="Something else.").system_prompt_hash != base.system_prompt_hash


@pytest.mark.parametrize("status", [Status.INIT, Status.RUNNING])
def test_result_rejects_non_terminal_status(status):
    with pytest.raises(ValidationError):
        AgentResult[EchoOutput](status=status, run_id="run-4", attempt_id="attempt-4")


@pytest.mark.parametrize(
    "declaration,field",
    [
        (Gemini(), "thinking"),
        (Claude(), "effort"),
        (OpenAI(), "effort"),
        (Gemma(), "thinking"),
        (StdioMCPServer(name="fs", command="npx"), "name"),
        (HttpMCPServer(name="web", url="https://example.com/mcp"), "name"),
        (Skill(name="notes", description="Notes.", instructions="One."), "name"),
    ],
    ids=lambda value: type(value).__name__ if not isinstance(value, str) else value,
)
def test_declarations_are_frozen(declaration, field):
    with pytest.raises(ValidationError):
        setattr(declaration, field, "other")


# an agent class built from data is checked as strictly as one written with `class` (docs/specs/agents.md)


def _tool(name: str):
    from langchain_core.tools import tool

    @tool(name)
    def a_tool() -> str:
        """A tool."""
        return name

    return a_tool


def _written(given: list):
    class Written(AgentBase):
        name: str = "x"
        description: str = "d"
        version: str = "1"
        prompt: str = "p"
        tools: list = given

    return Written


def _built(given: list):
    from pydantic import create_model

    return create_model(
        "Built",
        __base__=AgentBase,
        name=(str, "x"),
        description=(str, "d"),
        version=(str, "1"),
        prompt=(str, "p"),
        tools=(list, given),
    )


def _refusal(make, tools, **kwargs) -> str:
    with pytest.raises(ValidationError) as refused:
        make(tools)(**kwargs)
    return "; ".join(error["msg"] for error in refused.value.errors())


@pytest.mark.parametrize(
    ("tools", "kwargs", "says"),
    [
        ([_tool("same"), _tool("same")], {}, "duplicate names"),
        ([_tool("start_background")], {}, "start_background"),
        ([_tool("get_skill")], {}, "get_skill"),
        ([], {"recursion_limit": 0}, "greater than or equal to 1"),
    ],
    ids=["duplicate tool names", "a runner's tool name", "the skill tool's name", "recursion limit out of range"],
)
def test_a_class_built_from_data_is_refused_as_one_written_by_hand_is(tools, kwargs, says):
    by_hand = _refusal(_written, tools, **kwargs)
    from_data = _refusal(_built, tools, **kwargs)
    assert says in by_hand and by_hand.replace("Written", "X") == from_data.replace("Built", "X")


def test_a_class_built_from_data_that_is_well_formed_is_accepted_as_one_written_by_hand_is():
    assert _written([_tool("a"), _tool("b")])().name == _built([_tool("a"), _tool("b")])().name == "x"
