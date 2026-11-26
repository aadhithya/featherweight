"""Scripted fake models for the real-model tests (`E2E_FAKE`). The real chat model of the provider is built first, with a dummy
key, so that a setting the provider's class refuses fails here too. A test that has no script here fails."""

import base64
import json
import re
import socket
from typing import Any

from e2e_providers import PROVIDERS
from fakes import structured_reply, tool_reply
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from featherweight import Claude, Gemini, OpenAI


def spoiled(value: Any) -> Any:
    """A wrong answer of the same shape."""
    if isinstance(value, str):
        return "wrong"
    if isinstance(value, list):
        return ["wrong"]
    return 0 if isinstance(value, int) else value


def results(messages) -> list[str]:
    return [str(m.content) for m in messages if m.type == "tool"]


# for each test, its replies in order. A reply is a message, text to stream, or a function of the messages sent
def scripts(module) -> dict[str, list]:
    def answer(name: str, **args):
        return lambda messages: structured_reply(name, {k: v(messages) if callable(v) else v for k, v in args.items()})

    def code_in_the_pdf(messages) -> str:
        sent = re.search(r"'base64': '([A-Za-z0-9+/=]+)'", str([m.content for m in messages]))
        assert sent, "the attachment did not reach the model"
        code = re.search(rb"DOC-[0-9A-F]+", base64.b64decode(sent[1]))
        assert code, "the file that reached the model holds no code"
        return code[0].decode()

    child = {"text": "the article", "max_words": 20}
    summary = "A warehouse at Pier 4."
    stock = getattr(module, "STOCK", {})
    return {
        "test_an_agent_finds_mcp_tools_by_search_and_calls_them": [
            tool_reply("search_tools", {"keywords": ["add", "access note"]}, "s"),
            tool_reply("call_tool", {"name": "fake_add", "args_json": '{"a": 17, "b": 25}'}, "a"),
            tool_reply("call_tool", {"name": "fake_access_note", "args_json": "{}"}, "n"),
            answer("TaskOutput", sum=42, access_code="7421"),
        ],
        "test_an_agent_uses_mcp_tools_and_reads_what_they_return": [
            tool_reply("fake_add", {"a": 17, "b": 25}, "a"),
            tool_reply("fake_picture", {}, "p"),
            tool_reply("fake_access_note", {}, "n"),
            answer("TaskOutput", sum=42, colour="red", access_code="7421"),
        ],
        "test_an_agent_reads_its_skill_and_the_file_it_points_to": [
            tool_reply("get_skill", {"name": "meeting-notes"}, "1"),
            tool_reply("get_skill", {"name": "meeting-notes", "resource": "references/tags.md"}, "2"),
            answer("NotesOutput", notes=["[DECISION] The launch moved.", "[ACTION] Tom updates the plan."]),
        ],
        "test_an_agent_launches_another_agent_with_the_real_model": [
            tool_reply("launch_agent", {"agent_name": "summariser", "input_json": json.dumps(child)}, "l"),
            structured_reply("SummariserOutput", {"summary": summary}),
            answer("ReporterOutput", headline="Pier 4", summary=summary),
        ],
        "test_an_agent_calls_a_tool_and_returns_typed_output": [
            tool_reply("stock_code", {"item": "kettle"}, "k"),
            answer("Answer", item="kettle", code=lambda m: results(m)[-1]),
        ],
        "test_a_chat_streams_a_tool_call_and_goes_on_with_its_history": [
            tool_reply("stock_code", {"item": "kettle"}, "k"),
            lambda m: f"The kettle is {stock['kettle']}.",
            tool_reply("stock_code", {"item": "lamp"}, "l"),
            lambda m: f"Kettle {stock['kettle']}, lamp {stock['lamp']}.",
        ],
        "test_a_chat_reads_a_pdf_that_is_attached": [code_in_the_pdf],
    }


class Replies(BaseChatModel):
    """Replays a script, as a whole message or as a stream of one chunk."""

    left: list
    wrong: bool

    @property
    def _llm_type(self) -> str:
        return "e2e-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def next_reply(self, messages) -> AIMessage:
        item = self.left.pop(0)
        # only the last answer is spoiled: a child's reply stays right, and what the parent returns does not
        wrong = self.wrong and not self.left
        if callable(item):
            item = item(messages)
        if isinstance(item, str):
            return AIMessage(content="wrong" if wrong else item)
        if wrong and item.tool_calls:  # the typed output
            call = item.tool_calls[0]
            call = {**call, "args": {k: spoiled(v) for k, v in call["args"].items()}}
            return item.model_copy(update={"tool_calls": [call]})
        return item

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.next_reply(messages))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self.next_reply(messages)
        chunks = [
            {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
            for i, c in enumerate(reply.tool_calls)
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=reply.content, tool_call_chunks=chunks, id=f"turn-{len(self.left)}")
        )


def no_network(*args, **kwargs):
    raise AssertionError("E2E_FAKE: nothing may go over the network")


def install(request, monkeypatch, *, wrong: bool) -> None:
    for provider in PROVIDERS:
        for key in provider.keys:
            monkeypatch.setenv(key, "offline-dummy-key")  # a real key, if there is one, is not used
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    model = Replies(left=list(scripts(request.module)[request.node.originalname]), wrong=wrong)
    for settings in (Gemini, Claude, OpenAI):

        def build(self, real=settings.build):
            real(self)
            return model

        monkeypatch.setattr(settings, "build", build)
