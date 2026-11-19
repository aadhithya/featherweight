"""The tool registry: the tools of one attempt are checked together, and the model gets only valid, distinct names."""

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from featherweight.errors import SetupError
from featherweight.models import AgentBase
from featherweight.registry import DIRECT, DISCOVERABLE, ToolRegistry


def named(name: str, description: str = "A tool."):
    @tool(name, description=description)
    def one(x: str) -> str:
        return x

    return one


def test_the_registry_keeps_the_tools_and_their_names_in_the_order_they_were_added():
    registry = ToolRegistry()
    registry.add(named("first"), "local")
    registry.add(named("second"), "mcp")
    registry.add(named("third"), "runner")

    assert registry.names == ["first", "second", "third"]
    assert [t.name for t in registry.tools] == ["first", "second", "third"]


@pytest.mark.parametrize("name", ["a", "_a", "get_issue", "a.b:c-d_e", "x" * 64])
def test_names_the_model_can_be_given(name):
    ToolRegistry().add(named(name), "local")


@pytest.mark.parametrize("name", ["1abc", "has space", "slash/name", "-dash", "x" * 65])
def test_names_the_model_could_not_be_given_are_refused(name):
    with pytest.raises(SetupError, match="cannot be given to the model"):
        ToolRegistry().add(named(name), "mcp")


def test_an_agent_cannot_have_two_tools_with_one_name():
    class Doubled(AgentBase):
        name: str = "doubled"
        description: str = "d"
        version: str = "0.1.0"
        prompt: str = "p"
        tools: list = [named("same"), named("same")]

    with pytest.raises(ValidationError, match="duplicate names"):
        Doubled()


@pytest.mark.parametrize("extra", [{}, {"mode": DISCOVERABLE, "server": "far"}], ids=["direct", "discoverable"])
def test_two_tools_with_one_name_are_refused_in_either_mode_and_the_message_says_where_they_came_from(extra):
    registry = ToolRegistry()
    registry.add(named("lookup"), "local")
    with pytest.raises(SetupError, match=r"'lookup' \(local and mcp\)"):
        registry.add(named("lookup"), "mcp", **extra)


def test_the_registry_binds_direct_tools_and_keeps_discoverable_ones_apart():
    registry = ToolRegistry()
    registry.add(named("local_one"), "local")
    registry.add(named("far_find"), "mcp", mode=DISCOVERABLE, server="far")

    assert registry.names == ["local_one"] and [t.name for t in registry.tools] == ["local_one"]
    assert [e.tool.name for e in registry.discoverable] == ["far_find"]
    assert registry.entry("far_find").mode == DISCOVERABLE and registry.entry("local_one").mode == DIRECT
    assert registry.entry("missing") is None


def test_the_registry_searches_by_name_description_and_argument_names_and_rebuilds_after_an_add():
    registry = ToolRegistry()
    registry.add(named("far_issue_list", "Lists the issues of a repository"), "mcp", mode=DISCOVERABLE, server="far")
    registry.add(named("far_weather", "Forecast for a city"), "mcp", mode=DISCOVERABLE, server="far")

    assert [e.tool.name for e in registry.search(["issues"])] == ["far_issue_list"]
    assert [e.tool.name for e in registry.search(["city", "forecast"])] == ["far_weather"]
    assert registry.search(["x"]) != []  # the argument is called x, in both tools
    registry.add(named("far_code_search", "Search code"), "mcp", mode=DISCOVERABLE, server="far")
    assert "far_code_search" in [e.tool.name for e in registry.search(["code"])]
