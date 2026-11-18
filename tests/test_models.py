import pytest
from fakes import Echo, EchoOutput
from pydantic import ValidationError

from micro.models import (
    ASK_ALLOWED,
    ASK_FORBIDDEN,
    CONFLICTS_ALLOWED,
    CONFLICTS_FORBIDDEN,
    AgentResult,
    RunStatus,
)


def test_defaults():
    agent = Echo()
    assert agent.model_name == "gemini-3.5-flash-lite"
    assert agent.reasoning == "medium"
    assert agent.allow_user_questions is False
    assert agent.raise_conflicts is False


@pytest.mark.parametrize(
    "kwargs,allowed",
    [
        ({"model_name": "gemini-3.6-flash"}, True),
        ({"model_name": "gemini-3.5-flash-lite"}, True),
        ({"model_name": "gemini-3.8-flash"}, True),
        ({"reasoning": "high"}, True),
        ({"model_name": "gemini-3.5-flash"}, False),
        ({"model_name": "gpt-4o"}, False),
        ({"reasoning": "extreme"}, False),
    ],
)
def test_only_allow_listed_models_and_reasoning_levels(kwargs, allowed):
    if allowed:
        Echo(**kwargs)
    else:
        with pytest.raises(ValidationError):
            Echo(**kwargs)


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


@pytest.mark.parametrize("status", [RunStatus.INIT, RunStatus.RUNNING])
def test_result_rejects_non_terminal_status(status):
    with pytest.raises(ValidationError):
        AgentResult[EchoOutput](status=status, agent_run_id="run-4")
