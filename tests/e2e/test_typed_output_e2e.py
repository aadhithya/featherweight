"""The base of every agent, with the real model: it calls a tool, and its typed output holds what the tool returned. With
thinking off and on, because a provider can treat a tool and a reply schema differently when it thinks. The code is
random: the model can only have it from the tool.
"""

import secrets
from typing import ClassVar

import pytest
from langchain_core.tools import BaseTool, tool

from featherweight.models import AgentBase, AgentInput, AgentOutput, Status

STOCK = {item: f"STK-{secrets.token_hex(3).upper()}" for item in ("kettle", "lamp")}
ASKED: list[str] = []


@tool
def stock_code(item: str) -> str:
    """The stock code of an item in the shop."""
    ASKED.append(item.lower())
    return STOCK.get(item.lower(), "unknown item")


class Question(AgentInput):
    item: str


class Answer(AgentOutput):
    item: str = ""
    code: str = ""


class Clerk(AgentBase):
    name: str = "clerk"
    description: str = "Looks up a stock code"
    version: str = "0.1.0"
    prompt: str = "Look up the stock code of the item with the stock_code tool. Return the item and the exact code."
    tools: list[BaseTool] = [stock_code]
    input_model: ClassVar[type[AgentInput]] = Question
    output_model: ClassVar[type[AgentOutput]] = Answer


@pytest.mark.parametrize("reasoning", ["off", "low"])
async def test_an_agent_calls_a_tool_and_returns_typed_output(runner, provider, reasoning):
    ASKED.clear()
    result = await runner.run(provider.on(Clerk, reasoning), Question(item="kettle"))

    assert result.status == Status.SUCCESS, result.error
    assert ASKED[:1] == ["kettle"], ASKED
    assert result.output.code.strip() == STOCK["kettle"], result.output
