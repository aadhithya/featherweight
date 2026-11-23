"""`calculate` and the date tools. Pure functions, plus one run through the runner with a mocked LLM."""

import datetime as dt
from typing import ClassVar

import pytest
from fakes import FakeSettings, Scripted, runner_with, structured_reply, tool_reply
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from featherweight import tools as tools_module
from featherweight.fingerprint import tools_hash
from featherweight.model_settings import ModelSettings
from featherweight.models import AgentBase, AgentInput, AgentOutput, Status
from featherweight.tools import add_to_date, calculate, current_datetime, days_between


def calc(expression: str) -> str:
    return calculate.invoke({"expression": expression})


@pytest.fixture
def fixed_now(monkeypatch):
    now = dt.datetime(2026, 11, 5, 10, 15, 30, 123456, tzinfo=dt.UTC)
    monkeypatch.setattr(tools_module, "_utc_now", lambda: now)
    return now


# calculate


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("320000 + 85000 + 45000", "450000"),
        ("2 + 3 * 4", "14"),
        ("(2 + 3) * 4", "20"),
        ("10 / 4", "2.5"),
        ("7 // 2", "3"),
        ("2 ** 10", "1024"),
        ("-5 + 3", "-2"),
        ("450000 * 0.19", "85500"),
        ("0.1 + 0.2", "0.3"),  # exact, not 0.30000000000000004
        ("1 / 3", "0.3333333333333333333333333333"),
        ("  12 * 3  ", "36"),
        ("5 - 5", "0"),
    ],
)
def test_calculate_results(expression, expected):
    assert calc(expression) == expected


@pytest.mark.parametrize(
    "expression,message",
    [
        ("1 / 0", "Error: division by zero."),
        ("10 % 0", "Error: invalid operation"),
        ("0 / 0", "Error: invalid operation"),
        ("2 ** 1000", "exponent"),
        ("9 ** 9 ** 9", "exponent"),
        ("abs(-1)", "only plain numbers"),
        ("__import__('os').system('echo hi')", "Error:"),
        ("a + 1", "Error:"),
        ("'abc'", "Error:"),
        ("True + 1", "Error:"),
        ("(lambda: 1)()", "Error:"),
        ("1 +", "not a valid"),
        ("", "Error:"),
        ("\u20ac450,000 + 1", "Error:"),
        ("1e999", "Error:"),
        ("1 + " * 100 + "1", "Error:"),
    ],
)
def test_calculate_rejects_anything_but_arithmetic_and_says_what_went_wrong(expression, message):
    result = calc(expression)
    assert result.startswith("Error:") and message in result, result


# dates


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"date": "2026-11-05", "weeks": 4}, "2026-12-03"),
        ({"date": "2026-11-05", "days": -5}, "2026-10-31"),
        ({"date": "2026-11-05", "months": 3}, "2027-02-05"),
        ({"date": "2026-11-05", "years": 1, "months": 1, "weeks": 1, "days": 1}, "2027-12-13"),
        ({"date": "2024-01-31", "months": 1}, "2024-02-29"),  # clamped to the end of the month
        ({"date": "2023-01-31", "months": 1}, "2023-02-28"),
        ({"date": "2024-02-29", "years": 1}, "2025-02-28"),
        ({"date": "2026-01-15", "months": -2}, "2025-11-15"),
        ({"date": "2026-11-05"}, "2026-11-05"),
    ],
)
def test_add_to_date(kwargs, expected):
    assert add_to_date.invoke(kwargs) == expected


@pytest.mark.parametrize(
    "tool,args",
    [
        *[
            (add_to_date, {"date": bad, "days": 1})
            for bad in ("05.11.2026", "2026-13-01", "2026-02-30", "tomorrow", "")
        ],
        (add_to_date, {"date": "9999-12-31", "days": 1}),  # out of range
        (add_to_date, {"date": "2026-01-01", "years": 100000}),
        (days_between, {"start": "nope", "end": "2026-11-05"}),
    ],
)
def test_date_tools_return_an_error_message_for_bad_input(tool, args):
    result = tool.invoke(args)
    assert result.startswith("Error:"), result


def test_dates_relative_to_now(fixed_now):
    assert current_datetime.invoke({}) == "2026-11-05T10:15:30+00:00"  # UTC, no microseconds
    assert add_to_date.invoke({"weeks": 4}) == "2026-12-03"  # "four weeks from now"
    assert add_to_date.invoke({"date": "today", "days": 1}) == "2026-11-06"
    assert add_to_date.invoke({"date": "TODAY"}) == "2026-11-05"
    assert days_between.invoke({"start": "2026-11-01", "end": "today"}) == "4"


def test_days_between():
    assert days_between.invoke({"start": "2023-12-15", "end": "2024-01-20"}) == "36"
    assert days_between.invoke({"start": "2024-01-20", "end": "2023-12-15"}) == "-36"
    assert days_between.invoke({"start": "2026-11-05", "end": "2026-11-05"}) == "0"


# through the runner


class TaskInput(AgentInput):
    task: str


class TaskOutput(AgentOutput):
    result: str | None = None


class Helper(AgentBase):
    name: str = "helper"
    description: str = "Uses the calculate and date tools"
    version: str = "0.1.0"
    prompt: str = "Use the tools."
    model: ModelSettings = FakeSettings()
    tools: list[BaseTool] = [calculate, current_datetime, add_to_date, days_between]
    input_model: ClassVar[type[AgentInput]] = TaskInput
    output_model: ClassVar[type[AgentOutput]] = TaskOutput


async def test_an_agent_can_call_the_tools_through_the_runner(db, fixed_now):
    model = Scripted(
        script=[
            tool_reply("calculate", {"expression": "320000 + 85000 + 45000"}, "t1"),
            tool_reply("add_to_date", {"weeks": 4}, "t2"),
            tool_reply("calculate", {"expression": "1 / 0"}, "t3"),
            structured_reply("TaskOutput", {"result": "done"}),
        ]
    )
    runner = runner_with(db, model)

    result = await runner.run(Helper, TaskInput(task="x"))

    assert result.status == Status.SUCCESS
    outputs = [m.content for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert outputs == ["450000", "2026-12-03", "Error: division by zero."]
    assert model.bound[0][:4] == [
        "calculate",
        "current_datetime",
        "add_to_date",
        "days_between",
    ]  # what it was bound to
    assert (await db.list_attempts())[0].tools_hash == tools_hash(Helper().tools, [])
