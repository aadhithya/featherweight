"""A streamed chat with the real model: a tool call in the first turn, and a second turn that sends back what the provider
produced in the first, its tool calls and any reasoning included. A provider that does not take its own history back fails
here.
"""

import secrets

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool

from featherweight import ChatInput, StreamingAgent

STOCK = {item: f"STK-{secrets.token_hex(3).upper()}" for item in ("kettle", "lamp")}
ASKED: list[str] = []


@tool
def stock_code(item: str) -> str:
    """The stock code of an item in the shop."""
    ASKED.append(item.lower())
    return STOCK.get(item.lower(), "unknown item")


class Shop(StreamingAgent):
    name: str = "shop"
    description: str = "Answers about the shop"
    version: str = "0.1.0"
    prompt: str = "You help in a shop. Use stock_code for a stock code, and quote codes exactly. Be brief."
    tools: list[BaseTool] = [stock_code]


async def turn(runner, agent, messages) -> tuple[str, list]:
    run = runner.stream(agent, ChatInput.from_langchain(messages))
    text, errors = "", []
    async for event in run:
        if event.type.value == "TEXT_MESSAGE_CONTENT":
            text += event.delta
        elif event.type.value == "RUN_ERROR":
            errors.append(event.message)
    assert not errors, errors
    print(f"\n[turn] {text!r}")
    return text, list(run.new_messages)


async def test_a_chat_streams_a_tool_call_and_goes_on_with_its_history(runner, provider):
    ASKED.clear()
    agent = provider.on(Shop)
    first = HumanMessage("What is the stock code of the kettle?")

    text, produced = await turn(runner, agent, [first])
    assert "kettle" in ASKED and STOCK["kettle"] in text, text

    text, _ = await turn(runner, agent, [first, *produced, HumanMessage("And the lamp? Give me both codes.")])
    assert STOCK["lamp"] in text and STOCK["kettle"] in text, text
