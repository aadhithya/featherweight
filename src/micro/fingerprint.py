"""A hash of a whole agent declaration, recorded on every run as `agent_hash`.

It covers everything that defines the agent, so that two runs with the same hash used the same agent. It does not cover
the code inside tools or in `check_output`: `version` is the signal for that.
"""

import hashlib
import json
from typing import Any

from langchain_core.tools import BaseTool

from micro.models import AgentBase, AgentInput


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
    return _canonical(
        {
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
        }
    )


def agent_hash(agent: AgentBase) -> str:
    """SHA-256 of the agent's canonical declaration."""
    text = json.dumps(agent_declaration(agent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def input_hash(agent_input: AgentInput) -> str:
    """SHA-256 of the input as canonical JSON, so that equal inputs give equal hashes."""
    text = json.dumps(agent_input.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()
