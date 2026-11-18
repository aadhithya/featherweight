"""MCP tools: the declaration, the connections, what the model is given, and what happens when a server fails."""

import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest
from fakes import Echo, EchoInput, Reactive, Scripted, runner_with, structured_reply, tool_reply
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from pydantic import ValidationError

from micro.errors import SetupError
from micro.mcp import HttpMCPServer, StdioMCPServer
from micro.mcp_connections import Lease, MCPConnections, connection_config
from micro.mcp_tools import present
from micro.models import RunStatus

SERVER_SCRIPT = str(Path(__file__).parent / "mcp_fake_server.py")


def fake(name="fake", **kwargs) -> StdioMCPServer:
    return StdioMCPServer(name=name, command=sys.executable, args=(SERVER_SCRIPT,), **kwargs)


def worker(*servers, tools=(), name="worker"):
    """An agent class with the given servers and local tools."""
    server_list, tool_list, agent_name = list(servers), list(tools), name

    class Worker(Echo):
        name: str = agent_name
        tools: list[BaseTool] = tool_list
        mcp_servers: list[StdioMCPServer | HttpMCPServer] = server_list

    return Worker


def done():
    return structured_reply("EchoOutput", {"echoed": "ok"})


def shown(model) -> list[list]:
    """The content of every tool result the model was shown, in order, one list of blocks each."""
    seen: dict[str, list] = {}
    for messages in model.seen:
        for message in messages:
            if isinstance(message, ToolMessage):
                content = message.content
                seen[message.tool_call_id] = (
                    content if isinstance(content, list) else [{"type": "text", "text": content}]
                )
    return list(seen.values())


def texts(model) -> list[str]:
    return ["".join(block.get("text", "") for block in blocks) for blocks in shown(model)]


INPUT = EchoInput(text="go")


# the declaration


def test_a_server_name_is_lowercase_words_and_not_too_long():
    for bad in ("Fake", "my-server", "_x", "a__b", "x" * 21, ""):
        with pytest.raises(ValidationError):
            fake(name=bad)
    assert fake(name="my_server2").name == "my_server2"


def test_the_numbers_and_lists_of_a_server_are_checked():
    with pytest.raises(ValidationError):
        fake(timeout_s=0)
    with pytest.raises(ValidationError):
        fake(max_result_chars=0)
    with pytest.raises(ValidationError):
        fake(tools=("add", "add"))
    with pytest.raises(ValidationError):
        fake(tools=("",))
    with pytest.raises(ValidationError):
        StdioMCPServer(name="x", command=" ")
    with pytest.raises(ValidationError):
        StdioMCPServer(name="x", command="c", env_from=("",))
    with pytest.raises(ValidationError):
        HttpMCPServer(name="x", url="ftp://example.com")
    with pytest.raises(ValidationError):
        fake(unknown_field=1)


def test_a_server_is_frozen():
    server = fake()
    with pytest.raises(ValidationError):
        server.name = "other"


def test_servers_with_equal_connection_settings_share_a_key_whatever_their_name_or_tools():
    assert fake(name="a").connection_key() == fake(name="b", tools=("add",), timeout_s=5).connection_key()
    assert fake().connection_key() != fake(env_from=("X",)).connection_key()
    assert fake().connection_key() != StdioMCPServer(name="fake", command="other").connection_key()
    one = HttpMCPServer(name="h", url="https://x.test/mcp", headers_from={"A": "V1"})
    assert (
        one.connection_key()
        == HttpMCPServer(name="g", url="https://x.test/mcp", headers_from={"A": "V1"}).connection_key()
    )
    assert one.connection_key() != HttpMCPServer(name="h", url="https://x.test/mcp").connection_key()
    bearer = HttpMCPServer(name="h", url="https://x.test/mcp", bearer_from="T")
    assert (
        bearer.connection_key() != HttpMCPServer(name="h", url="https://x.test/mcp", bearer_from="U").connection_key()
    )


def test_an_agent_cannot_list_two_servers_with_one_name():
    with pytest.raises(ValidationError, match="duplicate names"):
        worker(fake(), fake())()


def test_an_agent_cannot_have_a_tool_with_a_name_the_runner_uses():
    @tool
    def launch_agent(x: str) -> str:
        """A clash."""
        return x

    with pytest.raises(ValidationError, match="the runner uses"):
        worker(tools=[launch_agent])()


# the connection settings, and secrets


def test_secrets_are_read_from_the_environment_when_the_connection_is_made(monkeypatch):
    monkeypatch.setenv("TOKEN_A", "secret-a")
    monkeypatch.setenv("TOKEN_B", "secret-b")
    stdio = connection_config(fake(env_from=("TOKEN_A",), cwd="/tmp"))
    assert stdio["env"] == {"TOKEN_A": "secret-a"} and stdio["cwd"] == "/tmp" and stdio["transport"] == "stdio"
    http = connection_config(
        HttpMCPServer(name="h", url="https://x.test/mcp", headers_from={"Authorization": "TOKEN_B"})
    )
    assert http["headers"] == {"Authorization": "secret-b"} and http["url"] == "https://x.test/mcp"
    assert "env" not in connection_config(fake())  # nothing given: the process gets the default environment


def test_a_bearer_token_is_sent_as_the_authorization_header(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("TOKEN_B", "other")
    server = HttpMCPServer(
        name="h", url="https://x.test/mcp", bearer_from="GH_TOKEN", headers_from={"X-Thing": "TOKEN_B"}
    )
    assert connection_config(server)["headers"] == {"X-Thing": "other", "Authorization": "Bearer tok"}
    monkeypatch.delenv("GH_TOKEN")
    with pytest.raises(SetupError, match="GH_TOKEN"):
        connection_config(server)


def test_bearer_from_and_an_authorization_header_cannot_both_be_given():
    with pytest.raises(ValidationError, match="Authorization"):
        HttpMCPServer(name="h", url="https://x.test/mcp", bearer_from="T", headers_from={"authorization": "V"})
    with pytest.raises(ValidationError):
        HttpMCPServer(name="h", url="https://x.test/mcp", bearer_from="")


def test_a_missing_variable_is_named_and_is_a_setup_error(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(SetupError, match="NOT_SET_ANYWHERE"):
        connection_config(fake(env_from=("NOT_SET_ANYWHERE",)))
    monkeypatch.setenv("EMPTY_ONE", "")
    with pytest.raises(SetupError, match="EMPTY_ONE"):
        connection_config(fake(env_from=("EMPTY_ONE",)))


# what the model is given


def text(value):
    return {"type": "text", "text": value}


def test_text_images_and_embedded_files_pass_through():
    image = {"type": "image", "base64": "AAAA", "mime_type": "image/png"}
    document = {"type": "file", "base64": "BBBB", "mime_type": "application/pdf"}
    assert present([text("hi"), image, document], None, 100) == [text("hi"), image, document]


def test_a_link_to_a_file_becomes_text_so_that_nothing_fetches_it():
    blocks = present([{"type": "file", "url": "https://x.test/a.pdf", "mime_type": "application/pdf"}], None, 100)
    assert blocks == [text("Resource link: https://x.test/a.pdf (application/pdf)")]
    blocks = present([{"type": "image", "url": "https://x.test/a.png"}], None, 100)
    assert blocks == [text("Resource link: https://x.test/a.png (unknown type)")]


def test_a_result_with_no_content_shows_its_structured_content():
    assert present([], {"structured_content": {"a": 1}}, 100) == [text('{\n  "a": 1\n}')]
    assert present([text("")], {"structured_content": {"a": 1}}, 100) == [text('{\n  "a": 1\n}')]
    assert present([text("kept")], {"structured_content": {"a": 1}}, 100) == [text("kept")]
    assert present([], None, 100) == [text("[the tool returned no content]")]


def test_the_text_cap_leaves_a_result_at_the_limit_alone_and_cuts_one_over_it():
    assert present([text("x" * 100)], None, 100) == [text("x" * 100)]
    cut = present([text("x" * 101)], None, 100)
    assert cut == [text("x" * 100), text("[truncated: 101 characters, showing the first 100]")]


def test_the_cap_counts_all_text_blocks_together_and_leaves_media_alone():
    image = {"type": "image", "base64": "AAAA", "mime_type": "image/png"}
    cut = present([text("a" * 60), image, text("b" * 60)], None, 100)
    assert cut == [text("a" * 60), image, text("b" * 40), text("[truncated: 120 characters, showing the first 100]")]


# running agents with MCP tools


async def test_the_tools_of_a_server_are_named_after_it_and_bound_to_the_model(db):
    model = Scripted(script=[tool_reply("fake_add", {"a": 2, "b": 3}, "c1"), done()])
    async with runner_with(db, model) as runner:
        result = await runner.run(worker(fake()), INPUT)

    assert result.status == RunStatus.SUCCESS
    assert texts(model) == ["5"]
    (row,) = await db.list_runs()
    assert {"fake_add", "fake_pid", "fake_boom"} <= set(row.tool_names)


async def test_the_allow_list_keeps_only_the_named_tools(db):
    model = Scripted(script=[done()])
    async with runner_with(db, model) as runner:
        await runner.run(worker(fake(tools=("add", "pid"))), INPUT)

    (row,) = await db.list_runs()
    assert sorted(row.tool_names) == ["fake_add", "fake_pid"]


async def test_a_tool_the_server_does_not_have_fails_the_attempt_without_a_retry(db):
    async with runner_with(db, Scripted(script=[done()])) as runner:
        result = await runner.run(worker(fake(tools=("add", "adds"))), INPUT)

    assert result.status == RunStatus.FAILED and result.attempts == 1
    assert "adds" in result.error and "It has:" in result.error and "add" in result.error


async def test_a_command_that_does_not_exist_fails_at_once(db):
    server = StdioMCPServer(name="ghost", command="no-such-program-here")
    async with runner_with(db, Scripted(script=[done()])) as runner:
        result = await runner.run(worker(server), INPUT)

    assert result.status == RunStatus.FAILED and result.attempts == 1
    assert "ghost" in result.error and "could not be started" in result.error
    (row,) = await db.list_runs()
    assert row.status == RunStatus.FAILED


async def test_a_missing_environment_variable_fails_at_once_and_names_the_variable(db, monkeypatch):
    monkeypatch.delenv("FAKE_TOKEN", raising=False)
    async with runner_with(db, Scripted(script=[done()])) as runner:
        result = await runner.run(worker(fake(env_from=("FAKE_TOKEN",))), INPUT)

    assert result.status == RunStatus.FAILED and result.attempts == 1 and "FAKE_TOKEN" in result.error


async def test_two_tools_with_one_name_fail_the_attempt_without_a_retry(db):
    @tool
    def fake_add(a: int, b: int) -> int:
        """A local tool that has the name of an MCP tool."""
        return a + b

    async with runner_with(db, Scripted(script=[done()])) as runner:
        result = await runner.run(worker(fake(), tools=[fake_add]), INPUT)

    assert result.status == RunStatus.FAILED and result.attempts == 1
    assert "fake_add" in result.error and "local" in result.error and "mcp" in result.error


async def test_a_local_tool_and_an_mcp_tool_work_in_one_attempt(db):
    @tool
    def shout(word: str) -> str:
        """Upper-case a word."""
        return word.upper()

    model = Scripted(
        script=[tool_reply("shout", {"word": "hi"}, "c1"), tool_reply("fake_add", {"a": 1, "b": 1}, "c2"), done()]
    )
    async with runner_with(db, model) as runner:
        result = await runner.run(worker(fake(), tools=[shout]), INPUT)

    assert result.status == RunStatus.SUCCESS and texts(model) == ["HI", "2"]


async def test_a_tool_error_reaches_the_model_as_text_and_the_run_goes_on(db):
    model = Scripted(script=[tool_reply("fake_boom", {}, "c1"), done()])
    async with runner_with(db, model) as runner:
        result = await runner.run(worker(fake()), INPUT)

    assert result.status == RunStatus.SUCCESS
    assert "kaboom" in texts(model)[0]


async def test_calls_in_one_turn_run_at_the_same_time_on_one_process(db):
    message = AIMessage(
        content="",
        tool_calls=[{"name": "fake_pid", "args": {"delay_s": 0.5}, "id": f"c{i}"} for i in range(4)],
    )
    model = Scripted(script=[message, done()])
    async with runner_with(db, model) as runner:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await runner.run(worker(fake()), INPUT)
        elapsed = loop.time() - started

    assert result.status == RunStatus.SUCCESS
    pids = set(texts(model))
    assert len(pids) == 1  # one process served all four
    assert elapsed < 1.9  # four calls of 0.5s one after another would take 2s and more


async def test_agents_with_equal_connection_settings_share_one_process(db):
    one = Scripted(script=[tool_reply("fake_pid", {}, "c1"), done()])
    two = Scripted(script=[tool_reply("other_pid", {}, "c1"), done()])
    async with runner_with(db, one) as runner:
        await runner.run(worker(fake(name="fake")), INPUT)
        runner._model_factory = lambda agent: two
        await runner.run(worker(fake(name="other", tools=("pid",))), INPUT)

    assert texts(one) == texts(two)


async def test_servers_with_different_settings_get_their_own_process(db):
    one = Scripted(script=[tool_reply("fake_pid", {}, "c1"), done()])
    two = Scripted(script=[tool_reply("fake_pid", {}, "c1"), done()])
    async with runner_with(db, one) as runner:
        await runner.run(worker(fake(name="fake")), INPUT)
        runner._model_factory = lambda agent: two
        await runner.run(worker(fake(name="fake", env_from=("PATH",))), INPUT)

    assert texts(one) != texts(two)


async def test_a_server_that_dies_gives_text_to_the_model_and_the_next_run_gets_a_new_connection(db):
    first = Scripted(script=[tool_reply("fake_pid", {}, "c1"), tool_reply("fake_die", {}, "c2"), done()])
    second = Scripted(script=[tool_reply("fake_pid", {}, "c1"), done()])
    async with runner_with(db, first) as runner:
        result = await runner.run(worker(fake()), INPUT)
        assert result.status == RunStatus.SUCCESS  # the run was not lost with the server
        assert "unavailable" in texts(first)[1]

        runner._model_factory = lambda agent: second
        result = await runner.run(worker(fake()), INPUT)

    assert result.status == RunStatus.SUCCESS
    assert texts(second)[0] != texts(first)[0]  # another process


async def test_a_call_over_the_timeout_gives_text_to_the_model(db):
    model = Scripted(script=[tool_reply("fake_pid", {"delay_s": 5}, "c1"), done()])
    async with runner_with(db, model) as runner:
        result = await runner.run(worker(fake(timeout_s=0.5)), INPUT)

    assert result.status == RunStatus.SUCCESS
    assert "timed out after 0.5s" in texts(model)[0]


async def test_a_long_result_is_cut_and_says_so(db):
    model = Scripted(script=[tool_reply("fake_big_text", {"length": 500}, "c1"), done()])
    async with runner_with(db, model) as runner:
        await runner.run(worker(fake(max_result_chars=100)), INPUT)

    (message,) = texts(model)
    assert message.startswith("x" * 100) and "[truncated: 500 characters, showing the first 100]" in message
    assert message.count("x") == 100


async def test_images_and_files_reach_the_model_and_a_link_becomes_text(db):
    model = Scripted(
        script=[
            tool_reply("fake_picture", {}, "c1"),
            tool_reply("fake_embedded", {}, "c2"),
            tool_reply("fake_link", {}, "c3"),
            tool_reply("fake_city", {}, "c4"),
            done(),
        ]
    )
    async with runner_with(db, model) as runner:
        result = await runner.run(worker(fake()), INPUT)

    assert result.status == RunStatus.SUCCESS
    image, document, link, city = shown(model)
    assert image[0]["type"] == "image" and image[0]["mime_type"] == "image/png" and image[0]["base64"]
    assert document[0]["type"] == "file" and document[0]["mime_type"] == "application/pdf"
    assert link == [text("Resource link: https://example.invalid/a.pdf (application/pdf)")]
    assert '"Oslo"' in city[0]["text"]


# connections


@pytest.fixture
def http_server():
    """A local HTTP server that answers every request with the given status."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    status = {"code": 401}
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(status["code"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/mcp", status, seen
    server.shutdown()


async def test_a_server_that_refuses_the_token_fails_at_once_and_names_the_server(db, http_server, monkeypatch):
    url, status, seen = http_server
    monkeypatch.setenv("FAKE_TOKEN", "tok-123")
    server = HttpMCPServer(name="remote", url=url, bearer_from="FAKE_TOKEN")
    async with runner_with(db, Scripted(script=[done()])) as runner:
        result = await runner.run(worker(server), INPUT)

    assert result.status == RunStatus.FAILED and result.attempts == 1
    assert "remote" in result.error and "401" in result.error and "refused" in result.error
    assert seen and seen[0] == "Bearer tok-123"  # the token went out as a bearer token
    assert "tok-123" not in result.error


async def test_a_server_that_is_busy_is_retried(db, http_server, monkeypatch):
    url, status, seen = http_server
    status["code"] = 429
    monkeypatch.setenv("FAKE_TOKEN", "tok-123")
    server = HttpMCPServer(name="remote", url=url, bearer_from="FAKE_TOKEN")
    async with runner_with(db, Scripted(script=[done()])) as runner:
        result = await runner.run(worker(server), INPUT)

    assert result.status == RunStatus.FAILED and result.attempts == 3


async def test_closing_the_runner_stops_the_server_process_and_can_be_done_twice(db):
    model = Scripted(script=[tool_reply("fake_pid", {}, "c1"), done()])
    runner = runner_with(db, model)
    async with runner:
        await runner.run(worker(fake()), INPUT)
        (pid,) = [int(value) for value in texts(model)]
        os.kill(pid, 0)  # still running between runs: the runner is open

    await runner.aclose()  # a second time
    await wait_gone(pid)


async def test_concurrent_first_leases_open_one_connection():
    connections = MCPConnections(keep_open=True)
    leases = [Lease(lambda: connections) for _ in range(4)]
    try:
        tools = await asyncio.gather(*(lease.list_tools(fake(name=f"s{i}")) for i, lease in enumerate(leases)))
        assert len(connections._entries) == 1
        assert all({"add", "pid"} <= {tool.name for tool in each} for each in tools)  # the server's own names
    finally:
        for lease in leases:
            await lease.release()
        await connections.aclose()
        await connections.aclose()


async def test_a_sub_agent_uses_the_servers_it_declares_itself(db):
    child_cls = worker(fake(), name="child_worker")
    parent_model = Scripted(
        script=[
            tool_reply("launch_agent", {"agent_name": "child_worker", "input_json": '{"text": "x"}'}, "p1"),
            done(),
        ]
    )
    child_model = Scripted(script=[tool_reply("fake_add", {"a": 4, "b": 5}, "k1"), done()])

    class Boss(Echo):
        name: str = "boss"
        subagents: list[type] = [child_cls]

    models = {"boss": parent_model, "child_worker": child_model}
    runner = runner_with(db, parent_model)
    runner._model_factory = lambda agent: models[agent.name]
    result = await runner.run(Boss, INPUT)  # the runner is not open: the child's server starts and stops by itself

    assert result.status == RunStatus.SUCCESS
    assert texts(child_model) == ["9"]
    rows = {row.agent_name: row for row in await db.list_runs()}
    assert "fake_add" in rows["child_worker"].tool_names and "fake_add" not in rows["boss"].tool_names


async def wait_gone(pid: int) -> None:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"the server process {pid} is still running")


def pid_run(db, runs=1):
    """A runner whose model asks for the server's pid on its first call and finishes on its second, and the model."""
    script = []
    for number in range(runs):
        script += [tool_reply("fake_pid", {}, f"c{number}"), done()]
    model = Scripted(script=script)
    return runner_with(db, model), model


async def test_without_async_with_the_server_stops_when_the_run_ends(db):
    runner, model = pid_run(db)
    result = await runner.run(worker(fake()), INPUT)

    assert result.status == RunStatus.SUCCESS
    (pid,) = [int(value) for value in texts(model)]
    await wait_gone(pid)
    assert runner._mcp is not None and runner._mcp._entries == {}


async def test_without_async_with_each_run_starts_its_own_server(db):
    runner, model = pid_run(db, runs=2)
    await runner.run(worker(fake()), INPUT)
    await runner.run(worker(fake()), INPUT)

    first, second = texts(model)
    assert first != second


async def test_inside_async_with_the_server_stays_up_between_runs_and_stops_at_the_end(db):
    runner, model = pid_run(db, runs=2)
    async with runner:
        await runner.run(worker(fake()), INPUT)
        await runner.run(worker(fake()), INPUT)

    first, second = texts(model)
    assert first == second
    await wait_gone(int(first))


async def test_parallel_runs_share_one_server_and_it_stops_after_the_last(db, caplog):
    caplog.set_level(logging.INFO)

    def respond(user_text, call_number):
        if call_number == 1:
            return tool_reply("fake_pid", {"delay_s": 0.3}, "c1")
        return done()

    model = Reactive(respond=respond)
    runner = runner_with(db, model)
    inputs = [EchoInput(text=name) for name in ("a", "b", "c")]
    results = await runner.run_many(worker(fake()), inputs)

    assert [r.status for r in results] == [RunStatus.SUCCESS] * 3
    assert caplog.text.count("mcp_open server=fake") == 1 and caplog.text.count("mcp_close server=fake") == 1
    assert runner._mcp is not None and runner._mcp._entries == {}


async def test_a_retry_does_not_start_the_server_again(db, caplog):
    caplog.set_level(logging.INFO)
    model = Scripted(script=[RuntimeError("model down"), done()])
    result = await runner_with(db, model).run(worker(fake()), INPUT)

    assert result.status == RunStatus.SUCCESS and result.attempts == 2
    assert caplog.text.count("mcp_open server=fake") == 1


async def test_a_lease_holds_a_server_until_the_last_one_is_released():
    connections = MCPConnections()
    first, second = Lease(lambda: connections), Lease(lambda: connections)
    await first.list_tools(fake(name="a"))
    await second.list_tools(fake(name="b"))
    (entry,) = connections._entries.values()
    assert entry.users == 2

    await first.release()
    assert len(connections._entries) == 1  # the second still uses it
    await second.release()
    assert connections._entries == {}  # nobody does


async def test_a_lease_gives_the_raw_tools_and_leaves_names_and_filters_to_the_agent(db):
    connections = MCPConnections()
    lease = Lease(lambda: connections)
    try:
        raw = await lease.list_tools(fake(name="x", tools=("add",)))  # the connection ignores the allow-list
        assert {"add", "pid", "boom"} <= {tool.name for tool in raw}

        from micro.registry import ToolRegistry

        registry = ToolRegistry()
        await registry.add_mcp(fake(name="x", tools=("add",)), lease)
        assert registry.names == ["x_add"]  # the agent's view: prefixed and filtered
    finally:
        await lease.release()


async def test_a_released_server_starts_again_when_a_lease_asks(db):
    connections = MCPConnections()
    for _ in range(2):
        lease = Lease(lambda: connections)
        await lease.list_tools(fake())
        assert len(connections._entries) == 1
        await lease.release()
        assert connections._entries == {}


# logging and secrets


async def test_each_call_and_each_open_and_close_is_logged(db, caplog):
    caplog.set_level(logging.INFO)
    model = Scripted(script=[tool_reply("fake_add", {"a": 1, "b": 2}, "c1"), done()])
    async with runner_with(db, model) as runner:
        await runner.run(worker(fake()), INPUT)
    (row,) = await db.list_runs()

    log = caplog.text
    assert "mcp_open server=fake transport=stdio" in log and "mcp_close server=fake" in log
    assert f"mcp_call agent_run_id={row.agent_run_id} server=fake tool=fake_add status=ok" in log


async def test_a_secret_is_in_no_log_no_row_and_no_error(db, monkeypatch, caplog):
    secret = "s3cr3t-value-123"
    monkeypatch.setenv("FAKE_TOKEN", secret)
    monkeypatch.delenv("OTHER_TOKEN", raising=False)
    caplog.set_level(logging.DEBUG)

    model = Scripted(script=[tool_reply("fake_has_env", {"name": "FAKE_TOKEN"}, "c1"), done()])
    async with runner_with(db, model) as runner:
        ok = await runner.run(worker(fake(env_from=("FAKE_TOKEN",))), INPUT)
        failed = await runner.run(worker(fake(name="two", env_from=("FAKE_TOKEN", "OTHER_TOKEN"))), INPUT)

    assert ok.status == RunStatus.SUCCESS and texts(model) == [f"set, {len(secret)} characters"]  # the server got it
    assert failed.status == RunStatus.FAILED and "OTHER_TOKEN" in failed.error
    everything = caplog.text + repr(await db.list_runs()) + repr(ok) + repr(failed)
    assert secret not in everything
