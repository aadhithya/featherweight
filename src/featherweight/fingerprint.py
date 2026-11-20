"""A hash of a whole agent declaration, recorded on every run as `agent_hash`.

It covers everything that defines the agent, so that two runs with the same hash used the same agent. It does not cover
the code inside tools or in `check_output`: `version` is the signal for that. It does cover the JSON schemas of the agent's
input and output models.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from featherweight.models import AgentBase, AgentOutput


def _tool(tool: BaseTool) -> dict[str, Any]:
    schema = tool.tool_call_schema  # what the model sees: injected arguments are left out
    args = schema if isinstance(schema, dict) else schema.model_json_schema()  # ty: ignore[missing-union-attribute]
    return {"name": tool.name, "description": tool.description, "args": args}


def _canonical(value: Any) -> Any:
    """Sort what has no order of its own, so that equal values give equal text."""
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((_canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def agent_declaration(agent: AgentBase, _seen: tuple[type, ...] = ()) -> dict[str, Any]:
    """The agent as a plain structure. A sub-agent already being described further up is named only."""
    seen = (*_seen, type(agent))
    subagents: list[Any] = []
    for sub in agent.subagents:
        if sub in seen:
            subagents.append({"cycle": sub.__qualname__})
        else:
            subagents.append(agent_declaration(sub(), seen))
    declaration: dict[str, Any] = {
        "name": agent.name,
        "version": agent.version,
        "description": agent.description,
        "prompt": agent.prompt,
        "recursion_limit": agent.recursion_limit,
        "allow_user_questions": agent.allow_user_questions,
        "raise_conflicts": agent.raise_conflicts,
        "model": {"class": type(agent.model).__qualname__, "provider": agent.model.provider}
        | agent.model.model_dump(mode="json"),  # the class too: two classes with equal fields are not one model
        "tools": [_tool(t) for t in agent.tools],
        "subagents": subagents,
        "skills": [s.model_dump(mode="json", by_alias=True) for s in agent.skills],
        "mcp_servers": [s.model_dump(mode="json") for s in agent.mcp_servers],
        "system_prompt": agent.build_system_prompt(),
        # Class attributes, not fields of the instance, so they must be named here: a changed field, type, description
        # or default of either model is another agent.
        "input_schema": type(agent).input_model.model_json_schema(),
        "output_schema": type(agent).output_model.model_json_schema(),
    }
    if agent.background_agents:  # only when there are some, so that an agent without any keeps the hash it had
        declaration["background_agents"] = [_background_entry(entry, seen) for entry in agent.background_agents]
    return _canonical(declaration)


def _background_entry(entry: Any, seen: tuple[type, ...]) -> dict[str, Any]:
    """An agent in `background_agents` as a plain structure; a job class by its declaration hash."""
    if issubclass(entry, AgentBase):
        return {"cycle": entry.__qualname__} if entry in seen else agent_declaration(entry(), seen)
    return {"job": entry.name, "version": entry.version, "job_hash": entry.job_hash()}


def agent_hash(agent: AgentBase) -> str:
    """SHA-256 of the agent's canonical declaration."""
    text = json.dumps(agent_declaration(agent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def input_hash(agent_input: BaseModel) -> str:
    """SHA-256 of the input (an agent's or a job's) as canonical JSON, so that equal inputs give equal hashes."""
    text = json.dumps(agent_input.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def output_hash(output: AgentOutput) -> str:
    """SHA-256 of the output as canonical JSON."""
    return _digest(output.model_dump(mode="json"))


def text_hash(text: str) -> str:
    """SHA-256 of a text (what a streaming run produced)."""
    return hashlib.sha256(text.encode()).hexdigest()


def tools_hash(tools: Sequence[BaseTool], runner_tool_names: Sequence[str]) -> str:
    """SHA-256 of the tools bound to the model: each one's name, description and argument schema, and the names of the tools
    the runner added. It says that the tools changed without saying what they are (a tool's name can be client-specific)."""
    declared = sorted((_tool(tool) for tool in tools), key=lambda tool: tool["name"])
    return _digest(_canonical({"tools": declared, "runner": sorted(runner_tool_names)}))


def error_fields(exc: BaseException) -> tuple[str, str]:
    """What the run log keeps of an error: its class name, and the SHA-256 of its message. The message can quote data (a
    validation error quotes the value, `check_output` is the user's code), so the log never keeps it."""
    return type(exc).__name__, text_hash(str(exc))


def job_hash(name: str, version: str, input_model: type[BaseModel], output_model: type[BaseModel]) -> str:
    """SHA-256 of a job's declaration: its name, its version, and the JSON schemas of its input and output."""
    return _digest(
        {
            "name": name,
            "version": version,
            "input_schema": input_model.model_json_schema(),
            "output_schema": output_model.model_json_schema(),
        }
    )
