"""Default tools. Import them and put them in an agent's `tools` allow-list.

    store_get / store_set        read and write the run store, scoped to the current job
    calculate                    exact arithmetic, for totals and subtotals
    current_datetime / add_to_date / days_between
                                 date operations (UTC, ISO 8601)

The store tools get the job and store from the run context, never from the model. All tools return
text and do not raise, so the model can read what went wrong and react.
"""

import ast
import calendar
import datetime as dt
import json
import logging
import math
import operator
import re
from collections.abc import Callable
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow

from langchain.tools import ToolRuntime, tool

from micro.context import RunContext

logger = logging.getLogger(__name__)

NO_JOB = "There is no job for this run, so there is no run store to use."


@tool
async def store_get(key: str, runtime: ToolRuntime[RunContext]) -> str:
    """Read a value saved earlier for this job under `key`, by you or by another agent."""
    ctx = runtime.context
    if ctx.job_id is None:
        return NO_JOB
    value = await ctx.store.get(ctx.job_id, key)
    logger.debug("store_get job_id=%s key=%s found=%s", ctx.job_id, key, value is not None)
    if value is None:
        return f"Nothing is stored under '{key}'."
    return value if isinstance(value, str) else json.dumps(value)


@tool
async def store_set(key: str, value: str, runtime: ToolRuntime[RunContext]) -> str:
    """Save a text value under `key` for this job, so you or other agents can read it later.

    Use JSON text for structured data. Saving again under the same key overwrites it.
    """
    ctx = runtime.context
    if ctx.job_id is None:
        return NO_JOB
    await ctx.store.set(ctx.job_id, key, value)
    logger.debug("store_set job_id=%s key=%s", ctx.job_id, key)
    return f"Saved '{key}'."


# calculate

MAX_EXPRESSION_CHARS = 200
MAX_EXPONENT = 100

_BINARY_OPS: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_number(node: ast.AST) -> Decimal:
    """Evaluate an arithmetic expression tree exactly, allowing only numbers and + - * / // % **."""
    if isinstance(node, ast.Expression):
        return _eval_number(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise ValueError("number out of range")
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_number(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left, right = _eval_number(node.left), _eval_number(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ValueError(f"exponent larger than {MAX_EXPONENT} is not allowed")
        return _BINARY_OPS[type(node.op)](left, right)
    raise ValueError("only plain numbers and + - * / // % ** with parentheses are allowed")


def _format_number(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


@tool
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression exactly, e.g. "320000 + 85000 + 45000" or "(450000 + 200000) * 0.19".

    Use plain numbers (no currency signs or thousands separators) and + - * / // % ** with parentheses.
    Returns the result as a plain number, or an error message.
    """
    if len(expression) > MAX_EXPRESSION_CHARS:
        return f"Error: expression is longer than {MAX_EXPRESSION_CHARS} characters."
    try:
        return _format_number(_eval_number(ast.parse(expression.strip(), mode="eval")))
    except SyntaxError:
        return "Error: not a valid arithmetic expression."
    except DivisionByZero:
        return "Error: division by zero."
    except InvalidOperation:
        return "Error: invalid operation (for example modulo zero, or 0 divided by 0)."
    except Overflow:
        return "Error: the number is too large."
    except (ValueError, ArithmeticError) as exc:
        return f"Error: {exc}"


# dates (UTC, ISO 8601)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_date(text: str) -> dt.date:
    """`YYYY-MM-DD`, or "today" for the current UTC date."""
    text = text.strip()
    if text.lower() == "today":
        return _utc_now().date()
    if not _ISO_DATE.fullmatch(text):
        raise ValueError(f"'{text}' is not a date in YYYY-MM-DD format")
    return dt.date.fromisoformat(text)


def _add_months(date: dt.date, months: int) -> dt.date:
    """Calendar-aware: the day is clamped to the end of the target month (Jan 31 + 1 month = Feb 28/29)."""
    year, month0 = divmod(date.year * 12 + date.month - 1 + months, 12)
    month = month0 + 1
    return dt.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


@tool
def current_datetime() -> str:
    """Get the current date and time in UTC as an ISO 8601 string, e.g. 2026-11-05T10:15:30+00:00."""
    return _utc_now().replace(microsecond=0).isoformat()


@tool
def add_to_date(date: str = "today", days: int = 0, weeks: int = 0, months: int = 0, years: int = 0) -> str:
    """Add (or, with negative numbers, subtract) time to a date and return it as YYYY-MM-DD.

    `date` is YYYY-MM-DD, or "today" (the default) for the current UTC date. Months and years are calendar
    based and clamp to the end of the month, e.g. 2024-01-31 plus 1 month is 2024-02-29.
    Example: four weeks from now is add_to_date(weeks=4).
    """
    try:
        shifted = _add_months(_parse_date(date), years * 12 + months)
        return (shifted + dt.timedelta(days=days, weeks=weeks)).isoformat()
    except (ValueError, OverflowError) as exc:
        return f"Error: {exc}"


@tool
def days_between(start: str, end: str) -> str:
    """Number of days from `start` to `end` (both YYYY-MM-DD, or "today"); negative if `end` is before `start`."""
    try:
        return str((_parse_date(end) - _parse_date(start)).days)
    except ValueError as exc:
        return f"Error: {exc}"
