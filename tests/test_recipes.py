"""The recipes run as written. The code of each page is executed with a fake model, no network and a temporary database; what is the
application's (a store, a directory, a web handler) is in the recipe itself or is supplied here, and the recipe says which. A recipe that
uses the agent of Getting started says so in a comment, and the agent is read from that page. The platform recipes with more to say are in
`test_recipes_platform.py` and `test_skill_package.py`."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fakes import Streams, structured_reply, tool_reply
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from mcp_helpers import fake as fake_mcp_server
from test_recipes_platform import RECIPES, blocks, run_block

import featherweight
from featherweight import AgentRunner, Claude, Gemini, OpenAI

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


# a different provider


async def test_an_agent_is_run_on_the_provider_that_its_model_names(recipes, capsys):
    recipes(PerAgent(scripts={"Rate how urgent": [structured_reply("Triage", {"urgency": "high"})]}))
    ns = await run_recipe("a-different-provider.md")

    assert "urgency='high'" in capsys.readouterr().out
    assert isinstance(ns["Triager"]().model, Claude) and ns["Triager"]().model.effort == "low"


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
    # The page names GitHub's server; the test gives it the fake server of the MCP tests, with the same name.
    monkeypatch.setattr(featherweight, "HttpMCPServer", lambda **kwargs: fake_mcp_server(name=kwargs["name"]))
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


# traces for jobs and agents


async def test_a_job_and_its_agents_are_traced(recipes, capsys, monkeypatch):
    import sys

    from opentelemetry.sdk.trace import export

    # The console exporter binds `sys.stdout` when its module is first imported, so a test that wants its output points it here.
    monkeypatch.setattr(
        export, "ConsoleSpanExporter", lambda original=export.ConsoleSpanExporter: original(out=sys.stdout)
    )
    assert says_it_uses_the_agent_of_getting_started("traces-for-jobs-and-agents.md")
    recipes(PerAgent(scripts={"List every person": [structured_reply("ExtractOutput", {"people": ["Anna Berger"]})]}))
    await run_recipe("traces-for-jobs-and-agents.md", getting_started=True)

    spans = capsys.readouterr().out
    assert "invoke_workflow" in spans and "invoke_agent" in spans and "chat" in spans


# a streaming agent for chat


async def test_a_turn_streams_as_server_sent_events_and_the_conversation_is_kept_by_you(recipes, capsys):
    recipes(Streams(turns=[["Hello", " there"]]))
    ns = await run_recipe("a-chat-agent-that-streams.md")

    out = capsys.readouterr().out
    assert "RUN_STARTED" in out and "Hello" in out and "RUN_FINISHED" in out
    kept = ns["history"]
    assert [type(m).__name__ for m in kept] == ["HumanMessage", "AIMessage"] and kept[1].content == "Hello there"


# a job that a worker runs


async def test_a_worker_runs_the_job_that_was_submitted(recipes, capsys):
    await run_recipe("a-job-that-a-worker-runs.md")

    out = capsys.readouterr().out
    assert "queued" in out and "succeeded" in out and "shouted='HELLO'" in out


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


async def test_a_failed_job_is_resumed_and_only_the_failed_step_runs_again(recipes, capsys):
    assert says_it_uses_the_agent_of_getting_started("resuming-a-failed-job.md")
    people = lambda *names: structured_reply("ExtractOutput", {"people": list(names)})  # noqa: E731
    down = RuntimeError("the model is down")
    model = recipes(
        PerAgent(
            scripts={"List every person": [people("Anna", "Tom"), down, down, down, people("Li")]}
            # the first job: one text answers, the other fails its three tries; the resume runs only that one, once
        )
    )
    await run_recipe("resuming-a-failed-job.md", getting_started=True)

    assert "FAILED then SUCCESS" in capsys.readouterr().out
    assert model.calls == {"List every person": 5}  # the step that had worked was not paid for again


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
    """The glossary says that the code of a recipe is run by the tests: a page that no test runs fails here."""
    tests = Path(__file__).parent
    text = "\n".join(p.read_text() for p in tests.glob("test_*.py"))
    for page in sorted(p.name for p in RECIPES.glob("*.md") if p.name != "README.md"):
        run = rf'(run_recipe|blocks|python_blocks)\(\s*(RECIPES / )?"{re.escape(page)}"'
        assert re.search(run, text), f"no test runs the code of {page}"
