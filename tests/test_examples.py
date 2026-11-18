"""The runnable examples at least build: their agents are valid definitions (sub-agents, skills, schemas). No model calls."""

import importlib.util
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent.parent / "examples"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / name / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_launching_example_has_an_editor_that_can_launch_exactly_two_agents():
    editor = load("launching").Editor()
    assert [sub().name for sub in editor.subagents] == ["summariser", "translator"]
    assert editor.model.model == "gemini-3.5-flash-lite"
    assert "- summariser:" in editor.build_system_prompt() and "- translator:" in editor.build_system_prompt()


def test_the_skills_example_has_an_assistant_with_the_three_skills():
    assistant = load("skills").Assistant()
    assert sorted(skill.name for skill in assistant.skills) == ["email-reply", "meeting-notes", "release-notes"]
    assert assistant.model.model == "gemini-3.5-flash-lite"


@pytest.mark.parametrize("name", ["launching", "skills", "mcp"])
def test_the_examples_ask_for_a_request_or_use_their_samples(name):
    module = load(name)
    assert module.SAMPLES and all(isinstance(sample, str) and sample for sample in module.SAMPLES)


def test_the_mcp_example_reads_github_with_a_token_by_name_and_six_read_only_tools():
    agent = load("mcp").RepoReader()
    (server,) = agent.mcp_servers
    assert server.url == "https://api.githubcopilot.com/mcp/readonly" and server.bearer_from == "GITHUB_TOKEN"
    assert server.tools is not None and len(server.tools) == 6 and server.discover is False
    assert not any(word in tool for tool in server.tools for word in ("create", "update", "delete", "merge", "push"))
    assert agent.model.model == "gemini-3.5-flash-lite"
