"""The tool registry: the tools of one attempt are checked together, and the model gets only valid, distinct names."""

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from micro.errors import SetupError
from micro.models import AgentBase
from micro.registry import ToolRegistry


def named(name: str):
    @tool(name)
    def one(x: str) -> str:
        """A tool."""
        return x

    return one


def test_the_registry_keeps_the_tools_and_their_names_in_the_order_they_were_added():
    registry = ToolRegistry()
    registry.add(named("first"), "local")
    registry.add(named("second"), "mcp")
    registry.add(named("third"), "runner")

    assert registry.names == ["first", "second", "third"]
    assert [t.name for t in registry.tools] == ["first", "second", "third"]


def test_two_tools_with_one_name_are_refused_and_the_message_says_where_they_came_from():
    registry = ToolRegistry()
    registry.add(named("lookup"), "local")
    with pytest.raises(SetupError, match=r"'lookup' \(local and mcp\)"):
        registry.add(named("lookup"), "mcp")


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
