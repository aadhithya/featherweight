"""A chat agent that streams, to run: a small web app with the three ways to talk to it, and a stop button.

    uv run uvicorn --factory --app-dir examples/chat agent:create_app

Needs ANTHROPIC_API_KEY and the `anthropic` extra, and `starlette` and `uvicorn` (they are in the dev group of this repository).
The agent is a support chat for an online shop, with one tool that looks up an order. It uses `claude-haiku-5-5` with thinking
off, so a conversation costs next to nothing; set `show_thoughts=True` and `thinking="adaptive"` to stream its thoughts too.

    POST /agui                the AG-UI protocol: the client sends the whole conversation
    POST /vercel              the Vercel AI SDK (`useChat`): the client sends the whole conversation
    POST /chat/{thread_id}    the server keeps the conversation: send {"message": "..."} and get AG-UI events back
    GET  /chat/{thread_id}    what the server kept, as AG-UI messages
    POST /stop/{run_id}       stop a response (the run id is in the first event, RUN_STARTED)

`micro` stores no conversation. The third endpoint shows how an application does it: load the history, stream, and save
`run.new_messages` when the stream is over, also when the client left halfway. The store here is a dict; use any LangChain
chat message history in its place.
"""

import contextlib
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from langchain_core.messages import BaseMessage, HumanMessage  # noqa: E402
from langchain_core.tools import BaseTool, tool  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from micro.adapters import BadRequest, agui, vercel  # noqa: E402
from micro.db import Database, default_db_path  # noqa: E402
from micro.log import setup_logging  # noqa: E402
from micro.messages import to_agui  # noqa: E402
from micro.model_settings import Claude, ModelSettings  # noqa: E402
from micro.runner import AgentRunner  # noqa: E402
from micro.streaming import ChatInput, StreamingAgent  # noqa: E402

ORDERS = {
    "1001": "Order 1001: a blue kettle, shipped on Monday, arrives Thursday.",
    "1002": "Order 1002: two mugs, being packed. It ships tomorrow.",
}


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by its number: what it holds and where it is."""
    return ORDERS.get(order_id, f"There is no order {order_id}.")


class Support(StreamingAgent):
    name: str = "support"
    description: str = "Answers customers of an online shop"
    version: str = "1"
    prompt: str = (
        "You are the support chat of an online shop. Answer in a sentence or two. "
        "To tell a customer where their order is, look it up with `lookup_order`. Never guess an order."
    )
    # The smallest Claude model, with thinking off and a small output limit: a conversation costs next to nothing.
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low", thinking="off", max_output_tokens=1000)
    tools: list[BaseTool] = [lookup_order]


class Store:
    """The conversations the server keeps, by thread id. A dict here; with LangChain, any `BaseChatMessageHistory`
    (`.messages` to load, `.add_messages(...)` to save) does the same job, in a database."""

    def __init__(self) -> None:
        self._threads: dict[str, list[BaseMessage]] = {}

    def messages(self, thread_id: str) -> list[BaseMessage]:
        return list(self._threads.get(thread_id, []))

    def add_messages(self, thread_id: str, messages: list[BaseMessage]) -> None:
        self._threads.setdefault(thread_id, []).extend(messages)


def make_app(runner: AgentRunner, store: Store | None = None, *, agent: type[StreamingAgent] = Support) -> Starlette:
    """The web app, for a runner that is already set up. `create_app` is the one for `uvicorn`."""
    store = store if store is not None else Store()

    def bad_request(exc: BadRequest) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=422)

    async def run_agui(request: Request) -> Response:
        try:
            chat_input = agui.parse_request(await request.body())
        except BadRequest as exc:
            return bad_request(exc)
        return StreamingResponse(
            agui.encode(runner.stream(agent, chat_input)), headers=agui.HEADERS, media_type=agui.CONTENT_TYPE
        )

    async def run_vercel(request: Request) -> Response:
        try:
            chat_input = vercel.parse_request(await request.body())
        except BadRequest as exc:
            return bad_request(exc)
        return StreamingResponse(vercel.encode(runner.stream(agent, chat_input)), headers=vercel.HEADERS)

    async def talk(request: Request) -> Response:
        """The server keeps the conversation: load it, stream a turn, save what the turn produced."""
        thread_id = request.path_params["thread_id"]
        try:
            text = str((await request.json())["message"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": 'send {"message": "..."}'}, status_code=422)
        user = HumanMessage(content=text)
        try:
            chat_input = ChatInput.from_langchain([*store.messages(thread_id), user], thread_id=thread_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        run = runner.stream(agent, chat_input)

        async def events() -> AsyncGenerator[str, None]:
            try:
                # `aclosing` closes the stream when the client leaves, which ends the run: only then is `new_messages` whole.
                async with contextlib.aclosing(agui.encode(run)) as pieces:
                    async for piece in pieces:
                        yield piece
            finally:
                store.add_messages(thread_id, [user, *run.new_messages])

        return StreamingResponse(events(), headers=agui.HEADERS, media_type=agui.CONTENT_TYPE)

    async def history(request: Request) -> Response:
        messages = to_agui(store.messages(request.path_params["thread_id"]))
        return JSONResponse([m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in messages])

    async def stop(request: Request) -> Response:
        stopped = runner.cancel(request.path_params["run_id"])
        return JSONResponse({"stopped": stopped}, status_code=200 if stopped else 404)

    return Starlette(
        routes=[
            Route("/agui", run_agui, methods=["POST"]),
            Route("/vercel", run_vercel, methods=["POST"]),
            Route("/chat/{thread_id}", talk, methods=["POST"]),
            Route("/chat/{thread_id}", history, methods=["GET"]),
            Route("/stop/{run_id}", stop, methods=["POST"]),
        ]
    )


def create_app() -> Starlette:
    """The app for `uvicorn --factory`: opens the run log and the runner when the server starts, and closes them at the end."""
    setup_logging(os.environ.get("MICRO_LOG_LEVEL") or "WARNING")
    db = Database(default_db_path())
    runner = AgentRunner(db)
    app = make_app(runner)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        async with db, runner:
            yield

    app.router.lifespan_context = lifespan
    return app


__all__ = ["Store", "Support", "create_app", "make_app"]
