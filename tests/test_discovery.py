"""Tool discovery: MCP tools found with `search_tools` and run with `call_tool`, instead of bound to the model."""

import json
import logging

import pytest
from fakes import Scripted, runner_with, tool_reply
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from mcp_helpers import INPUT, done, fake, shown, texts, worker
from pydantic import ValidationError

from featherweight.fingerprint import tools_hash
from featherweight.mcp import StdioMCPServer
from featherweight.models import Status


def finder(**kwargs) -> StdioMCPServer:
    """The fake server, found by search (the default for a server)."""
    return fake(discover=True, **kwargs)


def search(*keywords, call_id="s1"):
    return tool_reply("search_tools", {"keywords": list(keywords)}, call_id)


def call(name, args, call_id="c1"):
    return tool_reply(
        "call_tool", {"name": name, "args_json": json.dumps(args) if not isinstance(args, str) else args}, call_id
    )


def system_prompt(model) -> str:
    (prompt,) = {m.content for messages in model.seen for m in messages if isinstance(m, SystemMessage)}
    return prompt


async def run_with(db, model, agent):
    runner = runner_with(db, model)
    return await runner.run(agent, INPUT)


# the declaration, the prompt and the tools the model is given


def test_a_server_is_found_by_search_unless_it_opts_out():
    assert StdioMCPServer(name="x", command="c").discover is True
    assert fake().discover is False  # the test helper opts out


async def test_a_discovering_agent_is_bound_to_the_two_tools_only_and_the_prompt_names_the_server(db):
    model = Scripted(script=[done()])
    agent = worker(finder(description="Fake numbers and pictures"))
    result = await run_with(db, model, agent)

    assert result.status == Status.SUCCESS
    (row,) = await db.list_attempts()
    assert row.tools_hash == tools_hash([], ["search_tools", "call_tool"])  # nothing of the server is bound
    assert model.bound == [
        ["search_tools", "call_tool", "EchoOutput"]
    ]  # what the model was given, and its reply format
    prompt = system_prompt(model)
    assert "- fake: Fake numbers and pictures" in prompt and "search_tools" in prompt and "call_tool" in prompt
    assert "search again only when the tool you need is not in an earlier result" in prompt
    assert "fake_add" not in prompt  # the prompt names servers, not tools


async def test_without_a_description_the_prompt_names_the_server_alone(db):
    model = Scripted(script=[done()])
    await run_with(db, model, worker(finder()))
    prompt = system_prompt(model)
    assert "\n- fake\n" in prompt


async def test_an_agent_that_opts_out_binds_the_tools_and_has_neither_discovery_tool_nor_prompt(db):
    model = Scripted(script=[done()])
    await run_with(db, model, worker(fake(tools=("add",))))

    (row,) = await db.list_attempts()
    assert model.bound == [["fake_add", "EchoOutput"]]
    assert "search_tools" not in system_prompt(model)


async def test_one_agent_can_bind_one_server_and_search_another(db):
    model = Scripted(script=[done()])
    agent = worker(fake(name="near", tools=("add",)), finder(name="far"), tools=())
    await run_with(db, model, agent)

    (row,) = await db.list_attempts()
    assert model.bound == [["near_add", "search_tools", "call_tool", "EchoOutput"]]
    prompt = system_prompt(model)
    assert "- far" in prompt and "- near" not in prompt  # only the searched server is listed


async def test_the_prompt_hash_follows_discovery(db):
    on, off = worker(finder())(), worker(fake())()
    assert on.system_prompt_hash != off.system_prompt_hash
    assert on.has_discovery and not off.has_discovery


def test_an_agent_cannot_have_a_tool_with_a_discovery_name():
    @tool
    def search_tools(x: str) -> str:
        """A clash."""
        return x

    with pytest.raises(ValidationError, match="the runner uses"):
        worker(tools=[search_tools])()


# searching


async def test_a_search_ranks_the_matching_tools_first_and_gives_their_schemas(db):
    model = Scripted(script=[search("add", "numbers"), done()])
    await run_with(db, model, worker(finder()))

    (hits,) = [json.loads(text) for text in texts(model)]
    assert hits[0]["name"] == "fake_add" and hits[0]["server"] == "fake"
    assert hits[0]["description"] == "Add two numbers."
    assert set(hits[0]["input_schema"]["properties"]) == {"a", "b"}
    assert len(hits) <= 5


async def test_a_search_matches_the_names_of_arguments_and_returns_at_most_five_hits(db):
    model = Scripted(script=[search("delay"), search("fake", call_id="s2"), done()])
    await run_with(db, model, worker(finder()))

    by_argument, by_server = [json.loads(text) for text in texts(model)]
    assert by_argument[0]["name"] == "fake_pid"  # its argument is delay_s
    assert len(by_server) == 5  # every tool has the server's name, and there are more than five


async def test_a_search_with_no_match_says_so_and_lists_the_servers(db):
    model = Scripted(script=[search("zebra"), done()])
    await run_with(db, model, worker(finder()))

    (message,) = texts(model)
    assert "No tool matches ['zebra']" in message and "Servers you can search: fake" in message


async def test_a_search_with_no_keywords_asks_for_one(db):
    model = Scripted(script=[search(), search("  ", "!!", call_id="s2"), done()])
    await run_with(db, model, worker(finder()))

    assert all("Give at least one keyword" in text for text in texts(model))


async def test_only_the_tools_the_agent_may_use_can_be_found(db):
    model = Scripted(script=[search("pid", "picture", "boom", "add"), done()])
    await run_with(db, model, worker(finder(tools=("add",))))

    (hits,) = [json.loads(text) for text in texts(model)]
    assert [hit["name"] for hit in hits] == ["fake_add"]


# calling


async def test_a_found_tool_is_called_with_call_tool(db):
    model = Scripted(script=[search("add"), call("fake_add", {"a": 2, "b": 3}), done()])
    result = await run_with(db, model, worker(finder()))

    assert result.status == Status.SUCCESS
    assert texts(model)[1] == "5"


async def test_a_tool_can_be_called_without_a_search_because_the_allow_list_is_the_boundary(db):
    model = Scripted(script=[call("fake_add", {"a": 1, "b": 1}), done()])
    await run_with(db, model, worker(finder()))
    assert texts(model) == ["2"]


async def test_a_call_that_does_not_fit_the_schema_returns_the_errors_and_the_schema(db):
    model = Scripted(script=[call("fake_add", {"a": "x"}), done()])
    result = await run_with(db, model, worker(finder()))

    assert result.status == Status.SUCCESS
    (message,) = texts(model)
    assert "does not fit its schema" in message and "a: 'x' is not of type 'integer'" in message
    assert "b: 'b' is a required property" not in message and "'b' is a required property" in message
    assert '"required"' in message and '"properties"' in message  # the schema, so no new search is needed


async def test_input_that_is_not_json_or_not_an_object_is_explained(db):
    model = Scripted(script=[call("fake_add", "{a: 1"), call("fake_add", "[1, 2]", "c2"), done()])
    await run_with(db, model, worker(finder()))

    not_json, not_object = texts(model)
    assert "not valid JSON" in not_json and "must be a JSON object, not list" in not_object


async def test_an_unknown_name_and_a_bound_name_are_explained(db):
    model = Scripted(script=[call("nope", {}), call("near_add", {"a": 1, "b": 2}, "c2"), done()])
    await run_with(db, model, worker(finder(name="far"), fake(name="near", tools=("add",), env_from=("PATH",))))

    unknown, bound = texts(model)
    assert "'nope' is not a tool you can call. Search for it with search_tools." in unknown
    assert "'near_add' is bound to you directly: call it as 'near_add'." in bound


async def test_a_called_tool_keeps_the_timeout_the_cap_and_the_failure_as_text(db):
    model = Scripted(
        script=[
            call("fake_pid", {"delay_s": 10}, "c1"),
            call("fake_big_text", {"length": 500}, "c2"),
            call("fake_boom", {}, "c3"),
            done(),
        ]
    )
    # `timeout_s` is also the time the server has to start, which takes most of a second on a cold or slow machine
    result = await run_with(db, model, worker(finder(timeout_s=3, max_result_chars=100)))

    assert result.status == Status.SUCCESS
    timeout, big, boom = texts(model)
    assert "timed out after 3s" in timeout
    assert big.startswith("x" * 100) and "[truncated: 500 characters, showing the first 100]" in big
    assert "kaboom" in boom


async def test_an_image_from_a_called_tool_reaches_the_model(db):
    model = Scripted(script=[call("fake_picture", {}), done()])
    await run_with(db, model, worker(finder()))

    (blocks,) = shown(model)
    assert blocks[0]["type"] == "image" and blocks[0]["mime_type"] == "image/png"


async def test_each_search_and_each_call_is_logged(db, caplog):
    caplog.set_level(logging.INFO)
    model = Scripted(script=[search("add"), call("fake_add", {"a": 1, "b": 1}), call("fake_add", "{", "c2"), done()])
    await run_with(db, model, worker(finder()))
    (row,) = await db.list_attempts()

    log = caplog.text
    assert f"tool_search attempt_id={row.attempt_id} keywords=['add'] hits=" in log
    assert f"tool_call_via_search attempt_id={row.attempt_id} tool=fake_add status=ok" in log
    assert "tool_call_via_search" in log and "status=invalid" in log
    assert "mcp_call" in log  # the wrapper's own line, as for a direct call
