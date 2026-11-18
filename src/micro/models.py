"""Typed contracts for agents: the definition, its input/output base classes, and the run result."""

import hashlib
from enum import StrEnum
from typing import ClassVar, Generic, Literal, TypeVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

ModelName = Literal["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]
Reasoning = Literal["low", "medium", "high"]

ASK_ALLOWED = (
    "You may ask the user questions. If required information is missing or ambiguous, "
    "add a clear question to `open_questions` instead of guessing."
)
ASK_FORBIDDEN = (
    "Do not ask the user questions. Where information is missing, "
    "proceed with the most reasonable interpretation."
)
CONFLICTS_ALLOWED = (
    "If the input contains conflicting or inconsistent information, "
    "report each conflict in `open_questions` instead of silently picking one side."
)
CONFLICTS_FORBIDDEN = (
    "Do not report conflicts in the input; resolve any inconsistency with your best judgement."
)


SUBAGENTS = (
    "You can launch other agents to do part of the work. You may launch only these agents:\n{agents}\n"
    "Before you launch an agent, call `get_agent_schema` with its name to see the input it expects. Then call "
    "`launch_agent` with its name and that input as a JSON string. The result is the agent's output as JSON, or an "
    "error that says what went wrong; read it, and fix the input or carry on without that agent."
)


class RunStatus(StrEnum):
    INIT = "INIT"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class AgentInput(BaseModel):
    """Base for an agent's input. Agent-specific fields go on subclasses."""

    model_config = ConfigDict(extra="forbid")


class AgentOutput(BaseModel):
    """Base for an agent's structured output.

    Subclasses add their task fields, which should be optional (or allow empty values) so that
    a model that needs to ask a question is not forced to invent an answer to pass validation.
    """

    open_questions: list[str] = Field(
        default_factory=list,
        description="Questions or conflicts for the user. Never a failure condition.",
    )


class AgentBase(BaseModel):
    """Static definition of an agent. Each agent is a subclass; the runner instantiates it.

    Pydantic v2 rejects overriding an inherited field with an unannotated attribute, so
    subclasses re-annotate the fields they override.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    version: str
    prompt: str
    model_name: ModelName = "gemini-3.5-flash-lite"
    reasoning: Reasoning = "medium"
    tools: list[BaseTool] = Field(default_factory=list)
    # Guard against a runaway tool loop; passed to langgraph as `recursion_limit`.
    recursion_limit: int = Field(default=25, ge=1)
    # Not enforced: they only add an instruction to the system prompt.
    allow_user_questions: bool = False
    raise_conflicts: bool = False
    # The only agents this agent may launch (classes, not names; an agent cannot launch its own class). When this is
    # not empty, the runner gives the agent `launch_agent` and `get_agent_schema`, and the prompt lists them.
    subagents: list[type] = Field(default_factory=list)

    input_model: ClassVar[type[AgentInput]] = AgentInput
    output_model: ClassVar[type[AgentOutput]] = AgentOutput

    @model_validator(mode="after")
    def _check_subagents(self):
        names = []
        for sub in self.subagents:
            if not (isinstance(sub, type) and issubclass(sub, AgentBase)):
                raise ValueError(f"subagents must be AgentBase classes, got {sub!r}")
            if sub is type(self):
                raise ValueError(f"{self.name} lists itself in subagents: an agent can only launch other agents")
            names.append(sub().name)
        if len(set(names)) != len(names):
            raise ValueError(f"subagents of {self.name} have duplicate names: {names}")
        return self

    def check_output(self, agent_input: AgentInput, output: AgentOutput) -> None:
        """Check the output against the input, for rules the output schema alone cannot express.

        Raise `ValueError` to reject the output: the runner counts that as a failed attempt and retries.
        The default accepts everything.
        """

    @property
    def tool_names(self) -> list[str]:
        """What the run log stores; tool objects are not serialisable."""
        return [t.name for t in self.tools]

    @property
    def system_prompt_hash(self) -> str:
        """SHA-256 of the assembled system prompt; what the run log records."""
        return hashlib.sha256(self.build_system_prompt().encode()).hexdigest()

    def subagent_instance(self, name: str) -> "AgentBase | None":
        """The allowed sub-agent called `name`, as a fresh instance, or None."""
        for sub in self.subagents:
            agent = sub()
            if agent.name == name:
                return agent
        return None

    def build_system_prompt(self, *, can_launch: bool = True) -> str:
        """The prompt plus the conditional instructions derived from the two flags.

        With sub-agents, and unless `can_launch` is False (the runner sets it where the depth limit leaves the agent
        no launch tool), the prompt also lists the agents it may launch, by name and description. Their input
        schemas are not in the prompt: the agent asks for them with `get_agent_schema`.
        """
        parts = [
            self.prompt,
            ASK_ALLOWED if self.allow_user_questions else ASK_FORBIDDEN,
            CONFLICTS_ALLOWED if self.raise_conflicts else CONFLICTS_FORBIDDEN,
        ]
        if self.subagents and can_launch:
            listed = "\n".join(f"- {a.name}: {a.description}" for a in (sub() for sub in self.subagents))
            parts.append(SUBAGENTS.format(agents=listed))
        return "\n\n".join(parts)


OutT = TypeVar("OutT", bound=AgentOutput)


class AgentResult(BaseModel, Generic[OutT]):
    """What `AgentRunner.run` returns. The runner's status is kept separate from the model's output."""

    status: RunStatus
    output: OutT | None = None
    error: str | None = None
    agent_run_id: str
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking tokens
    attempts: int = Field(default=1, ge=1)
    latency_ms: float = 0.0  # total across attempts
    model_name: str | None = None  # as reported by the API, which can differ from the requested one

    @model_validator(mode="after")
    def _terminal_status_only(self):
        if self.status not in (RunStatus.SUCCESS, RunStatus.FAILED):
            raise ValueError("AgentResult status must be SUCCESS or FAILED")
        return self
