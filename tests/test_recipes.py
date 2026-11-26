"""The recipes run as written. The code of each page is executed with a fake model, no network and a temporary database; what is the
application's (a store, a directory, a web handler) is in the recipe itself or is supplied here, and the recipe says which. A recipe that
uses the agent of Getting started says so in a comment, and the agent is read from that page. The platform recipes with more to say are in
`test_recipes_platform.py` and `test_skill_package.py`."""

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fakes import Streams, structured_reply, tool_reply
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from mcp_helpers import fake as fake_mcp_server
from test_recipes_platform import RECIPES, blocks, run_block

import featherweight
from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, Claude, Gemini, ModelSettings, OpenAI

GETTING_STARTED = Path(__file__).parent.parent / "docs" / "getting-started.md"


def the_agent_of_getting_started() -> str:
    """The code block of Getting started that defines `PeopleExtractor`: recipes that use it say so, and are given it."""
    text = GETTING_STARTED.read_text()
    return next(b for b in re.findall(r"```python\n(.*?)```", text, re.S) if "class PeopleExtractor" in b)


class PerAgent(BaseChatModel):
    """A fake chat model with a script for each agent, found by what its system prompt says: agents that run together, in any order."""

    scripts: dict[str, list]
    calls: dict[str, int] = {}
    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "per-agent"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = " ".join(str(m.content) for m in messages if m.type == "system")
        key = next(k for k in self.scripts if k in system)
        number = self.calls.get(key, 0)
        self.calls[key] = number + 1
        self.seen.append(messages)
        item = self.scripts[key][number]
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


class QuickRunner(AgentRunner):
    """The runner of the recipes, without the pause between the tries of a failed run."""

    def __init__(self, db, **kwargs):
        kwargs.setdefault("retry_delay_s", 0.0)
        super().__init__(db, **kwargs)


@pytest.fixture
def recipes(monkeypatch, tmp_path):
    """Everything a recipe needs from outside: a database file in a temporary folder, a quick runner and a fake model."""
    monkeypatch.setenv("FTW_DB_PATH", str(tmp_path / "recipe.db"))
    monkeypatch.setattr(featherweight, "AgentRunner", QuickRunner)

    def use(model: BaseChatModel) -> BaseChatModel:
        for settings in (Claude, Gemini, OpenAI):
            monkeypatch.setattr(settings, "build", lambda self, model=model: model)
        return model

    return use


async def run_recipe(page: str, *, getting_started: bool = False, **names: Any) -> dict[str, Any]:
    """Run the code of a page as the reader would, then its `main()`. `names` is what the recipe says is yours."""
    namespace: dict[str, Any] = dict(names)
    code = "\n".join(([the_agent_of_getting_started()] if getting_started else []) + blocks(page))
    await run_block(code, namespace)
    await namespace["main"]()
    return namespace


def says_it_uses_the_agent_of_getting_started(page: str) -> bool:
    return "the agent of Getting started" in "\n".join(blocks(page))


def outcomes_of_run_job(monkeypatch) -> list:
    """The outcome of every `run_job` of a recipe, in order: a recipe may not print what it got."""
    seen: list = []
    real = featherweight.run_job

    async def spy(*args, **kwargs):
        outcome = await real(*args, **kwargs)
        seen.append(outcome)
        return outcome

    monkeypatch.setattr(featherweight, "run_job", spy)
    return seen


class ByInput(BaseChatModel):
    """A fake that replies by what the input of the agent contains, so that agents that run together get their own replies.
    The last reply of a list repeats."""

    answers: dict[str, list]
    calls: dict[str, int] = {}

    @property
    def _llm_type(self) -> str:
        return "by-input"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = str(next(m.content for m in messages if m.type == "human"))
        key = next(k for k in self.answers if k in user)
        number = self.calls.get(key, 0)
        self.calls[key] = number + 1
        item = self.answers[key][min(number, len(self.answers[key]) - 1)]
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


def people_reply(*names: str):
    return structured_reply("ExtractOutput", {"people": list(names)})


# a pipeline of two agents


async def test_the_pipeline_merges_the_people_of_every_text(recipes, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the recipe names its database file
    outcomes = outcomes_of_run_job(monkeypatch)
    model = recipes(ByInput(answers={"Anna met Tom.": [people_reply("Tom", "Anna")], "Li came.": [people_reply("Li")]}))

    await run_recipe("a-pipeline-of-two-agents.md", getting_started=True)

    [outcome] = outcomes
    assert outcome.status.value == "SUCCESS" and outcome.result.people == ["Anna", "Li", "Tom"]  # merged and sorted
    assert model.calls == {"Anna met Tom.": 1, "Li came.": 1}


async def test_the_pipeline_goes_on_without_a_text_whose_run_failed(recipes, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    outcomes = outcomes_of_run_job(monkeypatch)
    nobody = people_reply("Nobody")  # not in the text: `check_output` of the agent refuses it, at every try
    model = recipes(ByInput(answers={"Anna met Tom.": [nobody], "Li came.": [people_reply("Li")]}))

    await run_recipe("a-pipeline-of-two-agents.md", getting_started=True)

    [outcome] = outcomes
    assert outcome.status.value == "SUCCESS" and outcome.result.people == ["Li"]  # the failed run is skipped
    assert model.calls == {"Anna met Tom.": 3, "Li came.": 1}


# an agent that launches another


async def test_an_editor_launches_the_summariser_it_may_launch(recipes, capsys):
    model = recipes(
        PerAgent(
            scripts={
                "Write a headline": [
                    tool_reply(
                        "launch_agent", {"agent_name": "summariser", "input_json": '{"topic": "a new bridge"}'}, "c1"
                    ),
                    structured_reply("Headline", {"headline": "A bridge opens"}),
                ],
                "Summarise the topic": [structured_reply("Summary", {"summary": "A new bridge opens."})],
            }
        )
    )
    await run_recipe("an-agent-that-launches-another.md")

    assert "A bridge opens" in capsys.readouterr().out
    assert model.calls == {
        "Write a headline": 2,
        "Summarise the topic": 1,
    }  # the editor, the summariser, the editor again
    assert the_input_of(model, "Summarise the topic") == {"topic": "a new bridge"}
    [told] = tool_texts(model, "Write a headline")  # what came back to the editor
    assert json.loads(told)["status"] == "SUCCESS" and "A new bridge opens." in told
    editor_prompt = " ".join(str(m.content) for m in model.seen[0] if m.type == "system")
    assert "- summariser: Summarises a topic in one sentence" in editor_prompt  # what it may launch


# giving a sub-agent context

SUMMARY = "Shipped on Friday after the payment bug was fixed. Keep it short."


def launch_the_drafter(context: str, call_id: str = "c1"):
    input_json = json.dumps({"task": "Draft a thank-you note", "context": context})
    return tool_reply("launch_agent", {"agent_name": "drafter", "input_json": input_json}, call_id)


def tool_texts(model, key: str) -> list[str]:
    """The text of the tool results that the agent with this key was shown, in the order of the calls."""
    seen: list[str] = []
    for messages in model.seen:
        if key in " ".join(str(m.content) for m in messages if m.type == "system"):
            for m in messages:
                if m.type == "tool" and m.content not in seen:
                    seen.append(m.content)
    return seen


def the_input_of(model, key: str) -> dict:
    """The input (JSON) that the agent with this key was given, from its first call."""
    messages = next(m for m in model.seen if key in " ".join(str(x.content) for x in m if x.type == "system"))
    return json.loads([m.content for m in messages if isinstance(m, HumanMessage)][-1])


async def test_the_parent_writes_a_summary_in_the_childs_input_and_was_told_what_the_field_is_for(recipes, capsys):
    model = recipes(
        PerAgent(
            scripts={
                "Ask the drafter for a draft": [
                    tool_reply("get_agent_schema", {"agent_name": "drafter"}, "g1"),
                    launch_the_drafter(SUMMARY),
                    structured_reply("Reply", {"reply": "Thanks, team."}),
                ],
                "Draft the text for the task": [structured_reply("Draft", {"draft": "Thanks for Friday."})],
            }
        )
    )
    await run_recipe("giving-a-sub-agent-context.md")

    assert "Thanks, team." in capsys.readouterr().out
    schema, *_ = tool_texts(model, "Ask the drafter for a draft")
    assert "What you know that this task needs" in schema  # the description of the field is what the parent is shown
    assert the_input_of(model, "Draft the text for the task") == {"task": "Draft a thank-you note", "context": SUMMARY}


async def test_a_context_that_is_too_long_goes_back_to_the_parent_which_sends_a_shorter_one(recipes, capsys):
    model = recipes(
        PerAgent(
            scripts={
                "Ask the drafter for a draft": [
                    launch_the_drafter("x" * 500, "c1"),
                    launch_the_drafter(SUMMARY, "c2"),
                    structured_reply("Reply", {"reply": "Thanks, team."}),
                ],
                "Draft the text for the task": [structured_reply("Draft", {"draft": "Thanks for Friday."})],
            }
        )
    )
    await run_recipe("giving-a-sub-agent-context.md")

    first, second, *_ = tool_texts(model, "Ask the drafter for a draft")
    assert "is not valid" in first and "at most 400 characters" in first and "try again" in first
    assert model.calls["Draft the text for the task"] == 1  # the child ran once, with the short one
    assert json.loads(second)["status"] == "SUCCESS" and the_input_of(model, "Draft the text")["context"] == SUMMARY


async def test_the_same_field_works_for_a_background_job(recipes, tmp_path, capsys):
    from featherweight import SqliteDatabase, default_db_path

    model = recipes(
        PerAgent(
            scripts={
                "Start the drafter in the background": [
                    tool_reply(
                        "start_background",
                        {
                            "agent_name": "drafter",
                            "input_json": json.dumps({"task": "Draft a note", "context": SUMMARY}),
                        },
                        "s1",
                    ),
                    structured_reply("Reply", {"reply": "Started."}),
                    structured_reply("Reply", {"reply": "Done."}),  # once the result has been added
                ],
                "Draft the text for the task": [structured_reply("Draft", {"draft": "Thanks for Friday."})],
            }
        )
    )
    ns: dict[str, Any] = {}
    await run_block("\n".join(blocks("giving-a-sub-agent-context.md")), ns)

    class Backgrounder(AgentBase):
        name: str = "backgrounder"
        description: str = "Starts the drafter in the background"
        version: str = "0.1.0"
        prompt: str = "Start the drafter in the background, and go on."
        model: ModelSettings = Gemini()
        background_jobs: list[type] = [ns["Drafter"]]
        input_model: ClassVar[type[AgentInput]] = ns["Request"]
        output_model: ClassVar[type[AgentOutput]] = ns["Reply"]

    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        job_id = await db.create_job("background")
        result = await runner.run(Backgrounder, ns["Request"](request="Thank the team."), job_id=job_id)

    assert result.status.value == "SUCCESS" and model.calls["Draft the text for the task"] == 1
    assert (
        the_input_of(model, "Draft the text for the task")["context"] == SUMMARY
    )  # the job got the parent's summary too


# approving what an agent proposes

FOUND = [
    {"id": "f1", "airline": "TAP", "departs": "07:10", "price_eur": 142},
    {"id": "f2", "airline": "Lufthansa", "departs": "11:45", "price_eur": 189},
]


async def load_recipe(page: str) -> dict[str, Any]:
    """The code of a page, run, without its `main()`: for a test that uses what the page defines."""
    namespace: dict[str, Any] = {}
    await run_block("\n".join(blocks(page)), namespace)
    return namespace


def a_pending_proposal(ns: dict[str, Any]) -> dict:
    return ns["keep"](ns["Proposal"](options=[ns["Option"](**f) for f in FOUND], recommended="f1"))


async def test_the_agent_proposes_and_a_person_approves_once(recipes, capsys):
    model = recipes(
        PerAgent(
            scripts={
                "Search the flights for the trip": [
                    tool_reply("search_flights", {"origin": "MUC", "destination": "LIS", "day": "2026-12-12"}, "t1"),
                    structured_reply("Proposal", {"options": FOUND, "recommended": "f1"}),
                ]
            }
        )
    )
    ns = await run_recipe("approving-what-an-agent-proposes.md")

    out = capsys.readouterr().out
    assert "proposed ['f1', 'f2']" in out and "nothing is booked" in out
    assert "the same approval again is the same booking: True | bookings: 1" in out
    assert len(ns["BOOKINGS"]) == 1 and model.calls == {"Search the flights for the trip": 2}


async def test_the_agent_has_no_tool_that_books_and_the_executor_takes_no_arguments_of_the_action():
    ns = await load_recipe("approving-what-an-agent-proposes.md")

    assert [tool.name for tool in ns["FlightFinder"]().tools] == ["search_flights"]  # it reads: it cannot act
    assert set(inspect.signature(ns["approve"]).parameters) == {
        "proposal_id",
        "option_id",
        "shown_hash",
        "approver",
        "idempotency_key",
        "now",
    }  # an id, a choice and what was shown: never the flight, the price or any argument of the action


async def test_a_proposal_that_changed_expired_or_is_not_for_this_approver_books_nothing():
    from datetime import timedelta

    ns = await load_recipe("approving-what-an-agent-proposes.md")
    refused, approve = ns["NotApproved"], ns["approve"]

    record = a_pending_proposal(ns)
    with pytest.raises(refused, match="not the one that was shown"):
        approve(record["id"], "f1", "0" * 64, "u1", "k1")  # not what the person saw
    with pytest.raises(refused, match="may not approve"):
        approve(record["id"], "f1", record["hash"], "mallory", "k2")
    with pytest.raises(refused, match="not an option"):
        approve(record["id"], "f9", record["hash"], "u1", "k3")
    with pytest.raises(refused, match="expired"):
        approve(record["id"], "f1", record["hash"], "u1", "k4", now=record["expires_at"] + timedelta(minutes=1))
    assert record["status"] == "expired"
    with pytest.raises(refused, match="no pending proposal"):
        approve(record["id"], "f1", record["hash"], "u1", "k5")  # it is not pending any more

    stale = a_pending_proposal(ns)
    ns["FLIGHTS"].clear()  # the flight was sold in the meantime
    with pytest.raises(refused, match="no longer available"):
        approve(stale["id"], "f1", stale["hash"], "u1", "k6")
    assert ns["BOOKINGS"] == {}  # nothing was booked, by any of them


async def test_a_second_approval_with_another_key_does_not_book_again():
    ns = await load_recipe("approving-what-an-agent-proposes.md")
    record = a_pending_proposal(ns)

    ns["approve"](record["id"], "f1", record["hash"], "u1", "first")
    with pytest.raises(ns["NotApproved"], match="no pending proposal"):
        ns["approve"](record["id"], "f2", record["hash"], "u1", "second")
    assert len(ns["BOOKINGS"]) == 1


async def test_the_propose_tool_writes_a_proposal_and_books_nothing(recipes):
    from featherweight import SqliteDatabase, default_db_path

    ns = await load_recipe("approving-what-an-agent-proposes.md")
    model = recipes(
        PerAgent(
            scripts={
                "Help the person plan the trip": [
                    tool_reply("propose_booking", {"option_id": "f1"}, "p1"),
                    structured_reply("Reply", {"reply": "I proposed it: waiting for your approval."}),
                ]
            }
        )
    )
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        result = await runner.run(ns["Assistant"], ns["Search"](origin="MUC", destination="LIS", day="2026-12-12"))

    assert result.status.value == "SUCCESS"
    [record] = ns["PROPOSALS"].values()
    assert record["status"] == "pending" and ns["BOOKINGS"] == {}  # a proposal exists, and nothing was done
    told = [m.content for messages in model.seen for m in messages if m.type == "tool"]
    assert any("It is not booked" in str(text) for text in told)


# asking the user in the middle of a job


async def test_a_job_ends_with_its_questions_and_the_second_job_does_not_pay_for_the_first_stage(
    recipes, capsys, monkeypatch
):
    outcomes = outcomes_of_run_job(monkeypatch)
    model = recipes(
        PerAgent(
            scripts={
                "Plan the trip": [
                    structured_reply(
                        "Draft", {"plan": {"destination": "Lisbon"}, "open_questions": ["How many nights?"]}
                    )
                ],
                "Write the itinerary": [structured_reply("Itinerary", {"itinerary": "Four nights in Lisbon."})],
            }
        )
    )
    await run_recipe("asking-the-user-in-the-middle-of-a-job.md")

    out = capsys.readouterr().out
    assert "first job: SUCCESS | done: False | asks: ['How many nights?']" in out  # asking is a result
    assert "second job: SUCCESS | done: True | Four nights in Lisbon." in out
    assert model.calls == {"Plan the trip": 1, "Write the itinerary": 1}  # the planner ran once, across both jobs
    assert the_input_of(model, "Write the itinerary") == {
        "plan": {"destination": "Lisbon", "nights": 0},
        "answers": {"How many nights?": "4 nights"},
    }  # the finisher gets the plan of the first stage and what the person answered
    first, second = outcomes
    assert first.result.questions == ["How many nights?"] and second.job_id != first.job_id


# a different provider


async def test_an_agent_is_run_on_the_provider_that_its_model_names(recipes, capsys, monkeypatch):
    real_build = Claude.build
    recipes(PerAgent(scripts={"Rate how urgent": [structured_reply("Triage", {"urgency": "high"})]}))
    ns = await run_recipe("a-different-provider.md")

    assert "urgency='high'" in capsys.readouterr().out
    settings = ns["Triager"]().model
    assert isinstance(settings, Claude) and settings.effort == "low"
    # the settings that the page writes build the provider's real chat model (a dummy key: nothing is sent)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-key")
    chat = real_build(settings)
    assert type(chat).__name__ == "ChatAnthropic" and chat.model == "claude-haiku-5-5"


# telling a retry what went wrong


async def test_the_next_try_is_told_what_was_wrong_with_the_last(recipes, capsys):
    model = recipes(
        PerAgent(
            scripts={
                "Fill in the form": [
                    structured_reply("Filled", {"name": "Ada Lovelace", "year": 18}),  # two digits: the rule refuses it
                    structured_reply("Filled", {"name": "Ada Lovelace", "year": 1815}),
                ]
            }
        )
    )
    await run_recipe("telling-a-retry-what-went-wrong.md")

    assert "SUCCESS" in capsys.readouterr().out and model.calls == {"Fill in the form": 2}
    first, second = ([m.content for m in messages if isinstance(m, HumanMessage)][-1] for messages in model.seen)
    assert json.loads(first)["problems"] == []
    assert (
        "The year 18 is not a four-digit year" in json.loads(second)["problems"][0]
    )  # what went wrong travels in the input


# an agent with an MCP server


async def test_an_agent_with_an_mcp_server_uses_its_tools(
    recipes, capfd, monkeypatch
):  # capfd: the server is a subprocess
    # The page names GitHub's server. What it declares is checked as the real class takes it, and then the agent is given
    # the fake server of the MCP tests, with the same name.
    declared: list = []
    real_server = featherweight.HttpMCPServer

    def stand_in(**kwargs):
        declared.append(real_server(**kwargs))  # a field that the real class refuses fails here
        return fake_mcp_server(name=kwargs["name"])

    monkeypatch.setattr(featherweight, "HttpMCPServer", stand_in)
    model = recipes(
        PerAgent(
            scripts={
                "Answer from the repository": [
                    tool_reply("github_add", {"a": 2, "b": 3}, "t1"),
                    structured_reply("Answer", {"answer": "five"}),
                ]
            }
        )
    )
    ns = await run_recipe("an-agent-with-an-mcp-server.md")

    assert "answer='five'" in capfd.readouterr().out and model.calls == {"Answer from the repository": 2}
    assert ns["RepoReader"]().mcp_servers[0].name == "github"
    [server] = declared
    assert (server.url, server.bearer_from) == ("https://api.githubcopilot.com/mcp/readonly", "GITHUB_TOKEN")
    [told] = [m for m in model.seen[-1] if m.type == "tool"]
    assert "5" in str(told.content) and "not a tool" not in str(
        told.content
    )  # the server's own answer reached the model


# traces for jobs and agents


async def test_a_job_and_its_agents_are_traced(recipes, monkeypatch):
    from opentelemetry.sdk.trace import export
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    # The page prints to the console; the test keeps the spans, to look at how they nest and what they carry.
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(export, "ConsoleSpanExporter", lambda: exporter)
    assert says_it_uses_the_agent_of_getting_started("traces-for-jobs-and-agents.md")
    recipes(PerAgent(scripts={"List every person": [people_reply("Anna Berger")]}))
    await run_recipe("traces-for-jobs-and-agents.md", getting_started=True)

    spans = exporter.get_finished_spans()

    def the_span(prefix: str):
        [span] = [x for x in spans if x.name.startswith(prefix)]
        return span

    workflow, agent, chat = (the_span(prefix) for prefix in ("invoke_workflow", "invoke_agent", "chat"))
    assert workflow.parent is None
    assert agent.parent.span_id == workflow.context.span_id  # the agent is under its job
    assert chat.parent.span_id == agent.context.span_id  # and the model call under the agent
    # content is off unless asked for: neither the input nor the output is anywhere in the trace
    everything = " ".join(span.to_json() for span in spans)
    assert "Anna Berger" not in everything and "Tom Okafor" not in everything


# a streaming agent for chat


async def test_a_turn_streams_as_server_sent_events_and_the_conversation_is_kept_by_you(recipes, capsys):
    from featherweight import SqliteDatabase, default_db_path

    model = recipes(Streams(turns=[["Hello", " there"], ["It ships today."]]))
    ns = await run_recipe("a-chat-agent-that-streams.md")

    out = capsys.readouterr().out
    assert "RUN_STARTED" in out and "Hello" in out and "RUN_FINISHED" in out
    kept = ns["history"]
    assert [type(m).__name__ for m in kept] == ["HumanMessage", "AIMessage"] and kept[1].content == "Hello there"

    # the next turn is sent the whole conversation, and what it produced is kept too
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        pieces = [piece async for piece in ns["answer"](runner, HumanMessage("When does it ship?"))]

    assert [m.content for m in model.seen[1] if m.type in ("human", "ai")] == [
        "Where is my order?",
        "Hello there",
        "When does it ship?",
    ]
    assert "It ships today." in "".join(pieces)
    assert [m.content for m in kept] == ["Where is my order?", "Hello there", "When does it ship?", "It ships today."]


async def test_a_stopped_turn_keeps_what_was_said_for_the_next_one(recipes):
    from fakes import Hold

    from featherweight import SqliteDatabase, default_db_path

    gate = Hold()
    recipes(Streams(turns=[["Let me check ", gate, "the whole order."]]))
    ns = await load_recipe("a-chat-agent-that-streams.md")

    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:

        async def consume() -> str:
            return "".join([piece async for piece in ns["answer"](runner, HumanMessage("Where is my order?"))])

        task = asyncio.create_task(consume())
        await asyncio.wait_for(gate.reached.wait(), timeout=10)
        await asyncio.sleep(0.05)
        [row] = await db.list_attempts()
        assert runner.cancel(row.run_id) is True  # the one line the page names
        await asyncio.wait_for(task, timeout=10)

    assert [m.content for m in ns["history"]] == ["Where is my order?", "Let me check "]


# a job that a worker runs


async def test_a_worker_runs_the_job_that_was_submitted(recipes, capsys):
    await run_recipe("a-job-that-a-worker-runs.md")

    assert capsys.readouterr().out.splitlines() == ["queued", "running", "succeeded", "shouted='HELLO'"]


# a result store of your own


async def test_the_store_passes_the_kit_and_is_given_to_the_runner(recipes, capsys):
    ns = await run_recipe("a-result-store-of-your-own.md")

    assert "results kept by MyStore" in capsys.readouterr().out
    store = ns["MyStore"]()
    key = ns["ResultKey"](job_id="j", agent_hash="a" * 64, input_hash="b" * 64)
    await store.put(key, {"x": 1})
    assert await store.get(key) == {"x": 1}
    await store.delete_job("j")
    assert await store.get(key) is None


async def test_a_job_is_resumed_from_what_the_store_of_the_page_kept(recipes):
    from fakes import Echo, EchoInput

    from featherweight import JobContext, SqliteDatabase, job, run_job

    ns = await load_recipe("a-result-store-of-your-own.md")
    store = ns["MyStore"]()
    model = recipes(ByInput(answers={"hello": [structured_reply("EchoOutput", {"echoed": "hello"})]}))

    class Said(Echo):
        model: ModelSettings = Gemini()

    @job
    async def say(ctx: JobContext, input: EchoInput) -> EchoInput:
        result = await ctx.run(Said, input)
        return EchoInput(text=result.output.echoed)

    async with SqliteDatabase(":memory:") as db:
        runner = AgentRunner(db, results=store)
        first = await run_job(runner, say, EchoInput(text="hello"), resumable=True)
        assert first.status.value == "SUCCESS" and [k.job_id for k in store.results].count(first.job_id) >= 1
        second = await run_job(runner, say, EchoInput(text="hello"), resume_from=first.job_id)

    assert second.status.value == "SUCCESS" and second.result.text == "hello"
    assert model.calls == {"hello": 1}  # the second job read the step from the store


# an agent that starts another and goes on


async def test_the_researcher_starts_a_search_and_goes_on(recipes, capsys):
    model = recipes(
        PerAgent(
            scripts={
                "Answer the question. Start a web search": [
                    tool_reply(
                        "start_background",
                        {"agent_name": "web_searcher", "input_json": '{"query": "lighthouse"}'},
                        "s1",
                    ),
                    structured_reply("Report", {"report": "Still searching."}),  # it goes on without the result
                    structured_reply(
                        "Report", {"report": "The Pharos of Alexandria"}
                    ),  # once the result has been added
                ],
                "Search the web for the query": [structured_reply("Findings", {"findings": "The Pharos"})],
            }
        )
    )
    await run_recipe("an-agent-that-starts-another-and-goes-on.md")

    assert "The Pharos of Alexandria" in capsys.readouterr().out
    assert model.calls == {
        "Answer the question. Start a web search": 3,
        "Search the web for the query": 1,
    }  # the child ran as a job
    last = [m.content for m in model.seen[-1] if isinstance(m, HumanMessage)][-1]
    assert (
        "<background_result" in last and "The Pharos" in last
    )  # the child's result was added before the next model call


# resuming a failed job


async def test_a_failed_job_is_resumed_and_only_the_failed_step_runs_again(recipes, capsys, monkeypatch):
    from featherweight import SqliteDatabase, default_db_path

    assert says_it_uses_the_agent_of_getting_started("resuming-a-failed-job.md")
    outcomes = outcomes_of_run_job(monkeypatch)
    down = RuntimeError("the model is down")
    model = recipes(
        ByInput(
            answers={
                "Anna met Tom.": [people_reply("Anna", "Tom")],
                "Li came.": [down, down, down, people_reply("Li")],
            }
        )
    )
    await run_recipe("resuming-a-failed-job.md", getting_started=True)

    assert "FAILED then SUCCESS" in capsys.readouterr().out
    assert model.calls == {"Anna met Tom.": 1, "Li came.": 4}  # the step that had worked was not paid for again
    first, second = outcomes
    assert second.result.people == ["Anna", "Li", "Tom"]
    async with SqliteDatabase(default_db_path()) as db:
        rows = await db.list_attempts(job_id=second.job_id)
    [reused] = [row for row in rows if row.reused_from_attempt_id]
    assert (reused.input_tokens, reused.output_tokens) == (0, 0)
    assert len(rows) == 2


@pytest.mark.parametrize(
    "page", ["a-pipeline-of-two-agents.md", "resuming-a-failed-job.md", "traces-for-jobs-and-agents.md"]
)
def test_a_recipe_that_uses_the_people_extractor_says_where_it_comes_from(page):
    assert says_it_uses_the_agent_of_getting_started(page)


def test_the_pages_that_say_they_use_the_agent_of_getting_started_are_these_and_only_these():
    said = sorted(
        p.name
        for p in RECIPES.glob("*.md")
        if p.name != "README.md" and says_it_uses_the_agent_of_getting_started(p.name)
    )
    uses = sorted(
        p.name for p in RECIPES.glob("*.md") if "PeopleExtractor" in "\n".join(blocks(p.name)) and p.name != "README.md"
    )
    assert said == uses  # the ones that use it say so, and the ones that say so use it


def test_every_recipe_is_run_by_a_test():
    """The glossary says that the code of a recipe is run by the tests: a page that no test runs fails here. Only the files
    of the recipe tests count, so that a test which merely reads a page elsewhere does not. It looks for the call, and
    cannot tell what the test asserts."""
    tests = Path(__file__).parent
    text = "\n".join((tests / name).read_text() for name in ("test_recipes.py", "test_recipes_platform.py"))
    for page in sorted(p.name for p in RECIPES.glob("*.md") if p.name != "README.md"):
        if page == "testing-with-a-fake-model.md":  # it is a test file: test_skill_package.py runs it with pytest
            skill = (tests / "test_skill_package.py").read_text()
            assert f'python_blocks(RECIPES / "{page}")' in skill and "4 passed" in skill
            continue
        run = rf'(run_recipe|load_recipe|blocks)\(\s*"{re.escape(page)}"'
        assert re.search(run, text), f"no test runs the code of {page}"
