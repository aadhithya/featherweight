"""What goes wrong with typed output, refused (docs/specs/models.md#provider-limits): the schemas that Anthropic's
structured output does not take, by the model settings, when the agent is built; and a final answer that was given before the
model saw what its tools returned, by the runner, when the attempt ends. Fake models only, and no network."""

from typing import Any, ClassVar, Literal

import pytest
from fakes import FakeSettings, Scripted, runner_with
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, create_model

from featherweight import Claude, Gemini, ModelSettings, OpenAI, StreamingAgent
from featherweight.models import AgentBase, AgentInput, AgentOutput, Status

NATIVE = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"]


class Pair(BaseModel):
    key: str
    value: str


class Node(BaseModel):
    name: str
    children: list["Node"] = []


class Open(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""


class Price(BaseModel):
    amount: int


def output_of(**fields: Any) -> type[AgentOutput]:
    definitions: dict[str, Any] = {name: (kind, ...) for name, kind in fields.items()}
    return create_model("Out", __base__=AgentOutput, **definitions)


def agent_with(output: type[AgentOutput], settings: ModelSettings) -> AgentBase:
    class Filler(AgentBase):
        name: str = "filler"
        description: str = "Fills"
        version: str = "1"
        prompt: str = "Fill."
        model: ModelSettings = settings
        output_model: ClassVar[type[AgentOutput]] = output

    return Filler()


DICTIONARIES = {
    "dict": (dict, "answers"),
    "typed dict": (dict[str, str], "answers"),
    "optional dict": (dict[str, int] | None, "answers"),
    "list of dicts": (list[dict[str, str]], "answers[]"),
    "dict of models": (dict[str, Price], "answers"),
    "dict with fixed keys": (dict[Literal["a", "b"], str], "answers"),
    "model with extra fields": (Open, "answers"),
}


@pytest.mark.parametrize("model", NATIVE)
@pytest.mark.parametrize("kind", DICTIONARIES)
def test_a_free_form_object_is_refused_when_the_agent_is_built_and_the_field_is_named(model, kind):
    annotation, field = DICTIONARIES[kind]
    with pytest.raises(ValueError, match="would always come back empty") as refused:
        agent_with(output_of(answers=annotation), Claude(model=model))
    assert f"free-form objects ({field})" in str(refused.value) and model in str(refused.value)


def test_a_dictionary_inside_a_nested_model_is_found():
    class Inner(BaseModel):
        scores: dict[str, int]

    with pytest.raises(ValueError, match=r"free-form objects \(items\[\]\.scores\)"):
        agent_with(output_of(items=list[Inner]), Claude(model="claude-sonnet-5-5"))


def test_a_recursive_model_is_refused():
    with pytest.raises(ValueError, match="recursive model"):
        agent_with(output_of(root=Node), Claude(model="claude-sonnet-5-5"))


def optional_output(own: int) -> type[AgentOutput]:
    definitions: dict[str, Any] = {f"f{i}": (str, "") for i in range(own)}
    return create_model("Out", __base__=AgentOutput, **definitions)


def test_24_optional_fields_pass_and_25_are_refused_and_open_questions_is_one_of_them():
    sonnet = Claude(model="claude-sonnet-5-5")
    agent_with(optional_output(23), sonnet)
    with pytest.raises(ValueError, match="25 optional fields, and the API takes 24"):
        agent_with(optional_output(24), sonnet)


def test_the_optional_fields_of_nested_models_count_and_a_model_used_twice_counts_once():
    twenty: dict[str, Any] = {f"g{i}": (str, "") for i in range(20)}
    Wide: Any = create_model("Wide", **twenty)
    sonnet = Claude(model="claude-sonnet-5-5")
    agent_with(output_of(a=Wide, b=Wide, c=list[Wide]), sonnet)  # 20, and `open_questions`
    four: dict[str, Any] = {f"h{i}": (str, "") for i in range(4)}
    More = create_model("More", **four)
    with pytest.raises(ValueError, match="25 optional fields"):
        agent_with(output_of(a=Wide, b=More), sonnet)


def test_what_the_provider_takes_is_not_refused():
    class Fine(AgentOutput):
        pairs: list[Pair] = []
        level: Literal["low", "high"] | None = None
        either: Pair | str = ""
        score: int = Field(0, ge=0, le=10)

    agent_with(Fine, Claude(model="claude-sonnet-5-5"))


@pytest.mark.parametrize(
    "settings", [Claude(model="claude-haiku-5-5"), Gemini(), OpenAI()], ids=["haiku", "gemini", "openai"]
)
def test_the_other_models_are_not_held_to_these_limits(settings):
    class Wide(AgentOutput):
        answers: dict[str, str] = {}
        root: Node | None = None

    agent_with(Wide, settings)
    agent_with(optional_output(30), settings)


def test_a_streaming_agent_has_no_output_to_check():
    class Chat(StreamingAgent):
        name: str = "chat"
        description: str = "c"
        version: str = "1"
        prompt: str = "p"
        model: ModelSettings = Claude(model="claude-sonnet-5-5")

    Chat()


def test_a_model_that_a_user_adds_says_whether_its_output_is_native():
    from featherweight import ModelInfo

    class Mine(Claude):
        models: ClassVar[dict[str, ModelInfo]] = {
            "claude-next": ModelInfo(efforts=("low", "medium"), native_output=True)
        }

    with pytest.raises(ValueError, match="claude-next cannot fill Out"):
        agent_with(output_of(answers=dict[str, str]), Mine(model="claude-next"))


# the facts of the providers that the checks rest on: when one of these fails, a dependency changed, so look again


@pytest.mark.parametrize("model", list(Claude.models))
def test_the_catalog_says_native_output_where_langchain_uses_it(model, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")
    profile = Claude(model=model).build().profile or {}
    assert bool(profile.get("structured_output")) == Claude.models[model].native_output


def test_anthropics_sdk_still_empties_a_dictionary():
    from anthropic import transform_schema

    class Out(BaseModel):
        answers: dict[str, str]

    sent = transform_schema(Out)["properties"]["answers"]
    assert (sent["properties"], sent["additionalProperties"]) == ({}, False)


# the early answer


RAN: list[str] = []


@tool
def stock_code(item: str) -> str:
    """The stock code of an item."""
    RAN.append(item)
    return "STK-1"


class Question(AgentInput):
    item: str


class Answer(AgentOutput):
    code: str = ""


class Clerk(AgentBase):
    name: str = "clerk"
    description: str = "Looks up a code"
    version: str = "1"
    prompt: str = "Look it up."
    model: ModelSettings = FakeSettings()
    tools: list = [stock_code]
    input_model: ClassVar[type[AgentInput]] = Question
    output_model: ClassVar[type[AgentOutput]] = Answer


def calls(*named: tuple[str, dict]) -> AIMessage:
    made = [{"name": name, "args": args, "id": f"c{i}-{name}"} for i, (name, args) in enumerate(named)]
    return AIMessage(content="", tool_calls=made)


LOOK = ("stock_code", {"item": "kettle"})
TOGETHER = calls(LOOK, ("Answer", {}))


async def test_an_answer_given_together_with_a_tool_call_fails_the_attempt(db):
    RAN.clear()
    result = await runner_with(db, Scripted(script=[TOGETHER]), max_attempts=1).run(Clerk, Question(item="kettle"))

    assert result.status == Status.FAILED and result.output is None
    assert "EarlyAnswerError" in result.error and "calls to stock_code" in result.error
    assert RAN == ["kettle"]  # LangChain ran the tool before the attempt ended
    [row] = await db.list_attempts()
    assert row.error_type == "EarlyAnswerError"


async def test_it_is_retried_like_any_failed_attempt_and_the_tool_runs_again(db):
    RAN.clear()
    model = Scripted(script=[TOGETHER, calls(LOOK), calls(("Answer", {"code": "STK-1"}))])
    result = await runner_with(db, model).run(Clerk, Question(item="kettle"))

    assert result.status == Status.SUCCESS and result.output.code == "STK-1" and result.attempts == 2
    assert RAN == ["kettle", "kettle"]


async def test_an_answer_after_the_tools_result_is_accepted(db):
    model = Scripted(script=[calls(LOOK), calls(("Answer", {"code": "STK-1"}))])
    result = await runner_with(db, model, max_attempts=1).run(Clerk, Question(item="kettle"))

    assert result.status == Status.SUCCESS and result.attempts == 1
