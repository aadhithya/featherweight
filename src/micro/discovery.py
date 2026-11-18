"""`search_tools` and `call_tool`: how a model finds and runs the tools of an MCP server that are not bound to it.

They are built for each attempt, over the attempt's tool registry, and return text (or the content of a tool result) and
never raise into the run, so that the model can read what went wrong and carry on.
"""

import json
import logging
from typing import Any

from jsonschema import SchemaError
from jsonschema.validators import validator_for
from langchain_core.tools import BaseTool, tool

from micro.context import CURRENT_AGENT_RUN_ID
from micro.registry import DIRECT, ToolRegistry, input_schema
from micro.search import tokens

logger = logging.getLogger(__name__)

MAX_HITS = 5
MAX_SCHEMA_ERRORS = 5


def discovery_tools(registry: ToolRegistry) -> list[BaseTool]:
    """`search_tools` and `call_tool` over the discoverable tools of `registry`."""

    @tool
    async def search_tools(keywords: list[str]) -> str:
        """Find tools by keyword. Give a few words that describe what you need to do. Returns the best matches, each with
        its name, description and input schema."""
        if not any(tokens(keyword) for keyword in keywords):
            return "Give at least one keyword that describes what you need to do."
        hits = registry.search(keywords, MAX_HITS)
        logger.info("tool_search agent_run_id=%s keywords=%s hits=%d", CURRENT_AGENT_RUN_ID.get(), keywords, len(hits))
        if not hits:
            servers = ", ".join(sorted({entry.server for entry in registry.discoverable})) or "none"
            return f"No tool matches {keywords}. Try different or broader keywords. Servers you can search: {servers}."
        return json.dumps(
            [
                {
                    "name": entry.tool.name,
                    "server": entry.server,
                    "description": entry.tool.description,
                    "input_schema": input_schema(entry.tool),
                }
                for entry in hits
            ]
        )

    @tool
    async def call_tool(name: str, args_json: str) -> Any:
        """Run a tool you found with `search_tools`. `args_json` is the tool's input as a JSON string that follows its input
        schema. Returns what the tool returns, or an error that says what is wrong."""
        entry = registry.entry(name)
        if entry is None:
            return _logged(name, "invalid", f"'{name}' is not a tool you can call. Search for it with search_tools.")
        if entry.mode == DIRECT:
            return _logged(name, "invalid", f"'{name}' is bound to you directly: call it as '{name}'.")
        try:
            arguments = json.loads(args_json)
        except json.JSONDecodeError as exc:
            return _logged(name, "invalid", f"The input for '{name}' is not valid JSON: {exc}. Send a JSON object.")
        if not isinstance(arguments, dict):
            return _logged(
                name, "invalid", f"The input for '{name}' must be a JSON object, not {type(arguments).__name__}."
            )
        schema = input_schema(entry.tool)
        problems = _problems(schema, arguments)
        if problems:
            listed = "\n".join(f"- {problem}" for problem in problems)
            return _logged(
                name,
                "invalid",
                f"The input for '{name}' does not fit its schema:\n{listed}\nInput schema: {json.dumps(schema)}",
            )
        result = await entry.tool.ainvoke(
            arguments
        )  # the same wrapper as a direct call: timeout, cap, failures as text
        return _logged(name, "ok", result)

    return [search_tools, call_tool]


def _logged(name: str, status: str, result: Any) -> Any:
    logger.info("tool_call_via_search agent_run_id=%s tool=%s status=%s", CURRENT_AGENT_RUN_ID.get(), name, status)
    return result


def _problems(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """What is wrong with `arguments` against `schema`, at most a few, or none. A schema that is itself invalid cannot
    judge, so it accepts: the server will."""
    try:
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
    except SchemaError:
        return []
    errors = sorted(validator_class(schema).iter_errors(arguments), key=lambda error: list(error.absolute_path))
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or 'input'}: {error.message}"
        for error in errors[:MAX_SCHEMA_ERRORS]
    ]
