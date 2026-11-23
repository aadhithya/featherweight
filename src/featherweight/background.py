"""Background jobs: the result of a child comes back to its parent as a message.

A child that was started with `start_background` is a job of its own. When it finishes, its result cannot be a second result of the
call that started it (a provider rejects a tool result with no call), so it comes back as **a user message**:

    <background_result job_id="..." agent="..." status="SUCCESS">
    { the output, as JSON }
    </background_result>

While the parent's loop runs, `BackgroundResults` adds the message of each child that has finished before the parent's next model call, once
for each, after the results of the round's tool calls. A run that is not a chat also *waits*: when its model would end the run while a child is
still running, it waits for the child, adds its result, and lets the model go on. A chat turn does not wait: the application does it (see
`AgentRunner.background_message`). See docs/specs/background-agents.md.
"""

import asyncio
import html
import json
import logging
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from featherweight.db import FINAL, Database, JobRow
from featherweight.events import wait_for_job
from featherweight.models import RunStatus
from featherweight.results import ResultKey, ResultStore

if TYPE_CHECKING:
    from featherweight.runner import _Slot

logger = logging.getLogger("featherweight.background")


async def _stored_output(results: ResultStore | None, row: JobRow) -> str | None:
    """The output of a job that succeeded, as JSON, from the user's result store. `None` when there is none to give: no store, a job
    that kept none, a store that lost it or fails (that is logged, and never raised)."""
    if results is None or row.job_hash is None or row.input_hash is None:
        return None
    try:
        stored = await results.get(ResultKey(job_id=row.job_id, agent_hash=row.job_hash, input_hash=row.input_hash))
    except Exception:
        logger.exception("background_result_unreadable job_id=%s", row.job_id)
        return None
    return None if stored is None else json.dumps(stored)


async def background_result_message(db: Database, results: ResultStore | None, job_id: str) -> HumanMessage:
    """The user message that tells a model how a background job ended: its status, the type of its error if it failed, and its output
    if it succeeded (read from the result store). A job that succeeded and whose result is gone says `output="missing"`. `LookupError`
    for a job that does not exist."""
    row = await db.get_job(job_id)
    if row is None:
        raise LookupError(f"No such job: {job_id}")
    attributes = {"job_id": job_id, "agent": row.name, "status": row.status.value}
    if row.status is RunStatus.FAILED and row.error_type:
        attributes["error_type"] = row.error_type
    output = await _stored_output(results, row) if row.status is RunStatus.SUCCESS else None
    if row.status is RunStatus.SUCCESS and output is None:
        attributes["output"] = "missing"
    opening = "<background_result " + " ".join(f'{k}="{html.escape(v, quote=True)}"' for k, v in attributes.items())
    if output is None:
        return HumanMessage(content=opening + " />")
    return HumanMessage(content=f"{opening}>\n{output}\n</background_result>")


class BackgroundChildren:
    """The background children of one attempt of one agent: the jobs it started, and which of them it has been told about.

    `db` and `results` are where the children live: the runner's, or those of the `JobClient` that submits them to workers."""

    def __init__(self, db: Database, results: ResultStore | None):
        self.db = db
        self.results = results
        self.started: list[str] = []
        self.injected: set[str] = set()  # the children whose result was given to the model
        self._final: set[str] = set()  # the children known to have ended

    def add(self, job_id: str) -> None:
        self.started.append(job_id)

    async def live(self) -> int:
        """How many of the children have not ended."""
        live = 0
        for job_id in self.started:
            if job_id in self._final:
                continue
            row = await self.db.get_job(job_id)
            if row is not None and row.status in FINAL:
                self._final.add(job_id)
            elif row is not None:
                live += 1
        return live

    def outstanding(self) -> list[str]:
        """The children whose result the model has not been given yet."""
        return [job_id for job_id in self.started if job_id not in self.injected]

    async def collect(self) -> list[HumanMessage]:
        """The messages of the children that have ended and were not given yet, once each, in the order they were started. A database
        that fails is logged and gives none: an observer-like duty, it never fails the run."""
        messages: list[HumanMessage] = []
        try:
            for job_id in self.outstanding():
                row = await self.db.get_job(job_id)
                if row is None or row.status not in FINAL:
                    continue
                self._final.add(job_id)
                self.injected.add(job_id)  # before it is built: a message is given once, whatever happens next
                messages.append(await background_result_message(self.db, self.results, job_id))
        except Exception:
            logger.exception("background_collect_failed")
        return messages

    async def wait(self, slot: "_Slot | None") -> list[HumanMessage]:
        """Wait until a child that was not given yet has ended, and return the messages of the ones that have. Empty when there is none to
        wait for. The attempt hands its concurrency slot back while it waits, because a child that runs here needs one."""
        messages = await self.collect()
        if messages or not self.outstanding():
            return messages
        if slot is not None:
            slot.leave()
        try:
            waiters = [asyncio.ensure_future(wait_for_job(self.db, job_id)) for job_id in self.outstanding()]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
        finally:
            if slot is not None:
                await slot.back()
        return await self.collect()


def _would_end(messages: list[BaseMessage]) -> bool:
    """Whether the agent loop ends after the model's last reply: it asked for no tool, or every tool it asked for has its result (the
    structured answer of the agent is a tool call that is answered at once)."""
    last_ai: AIMessage | None = None
    answered: set[str] = set()
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            answered.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            last_ai = message
            break
    if last_ai is None:
        return False
    return all(call["id"] in answered for call in last_ai.tool_calls)


class BackgroundResults(AgentMiddleware):
    """The hooks of a parent's agent loop that give it the results of its background children.

    Before each model call, the messages of the children that ended since the last call are added, after the results of the tool calls of
    the round (this hook runs once they are in the state, so a result is never between a call and its result). With `wait`, when the
    model's reply would end the loop while a child still runs, the hook waits for the next one, adds its message, and sends the loop back to
    the model; the loop is still bounded by `recursion_limit`."""

    def __init__(self, children: BackgroundChildren, slot: "_Slot | None" = None, *, wait: bool = False):
        super().__init__()
        self.children = children
        self.slot = slot
        self.wait = wait

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = await self.children.collect()
        return {"messages": messages} if messages else None

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if not self.wait or not _would_end(state["messages"]):
            return None
        messages = await self.children.wait(self.slot)
        return {"messages": messages, "jump_to": "model"} if messages else None
