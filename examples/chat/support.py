"""The agent both chat examples use: a support chat for an online shop, with one tool."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from langchain_core.tools import BaseTool, tool  # noqa: E402

import featherweight as ftw  # noqa: E402

ORDERS = {"1001": "a blue kettle, shipped Monday, arrives Thursday", "1002": "two mugs, ships tomorrow"}


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by its number: what it holds and where it is."""
    return ORDERS.get(order_id, "There is no such order.")


class Support(ftw.StreamingAgent):
    name: str = "support"
    description: str = "Answers customers of an online shop"
    version: str = "1"
    prompt: str = (
        "You are the support chat of an online shop. Answer in a sentence or two. Look orders up, never guess."
    )
    model: ftw.ModelSettings = ftw.Claude(
        model="claude-haiku-5-5", effort="low", thinking="off", max_output_tokens=1000
    )
    tools: list[BaseTool] = [lookup_order]
