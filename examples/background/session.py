"""A streaming agent that starts a background job and goes on talking, and a session that wakes it when the work is done.

    python examples/background/session.py

Needs ANTHROPIC_API_KEY and the `anthropic` extra. Ask it to research something, keep chatting, and wait: when the researcher
finishes, the session starts the next turn by itself. An empty line leaves.
"""

import asyncio
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from ag_ui.core import CustomEvent, TextMessageContentEvent  # noqa: E402
from langchain_core.messages import BaseMessage, HumanMessage  # noqa: E402

from featherweight import (  # noqa: E402
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    ChatInput,
    Claude,
    Database,
    ModelSettings,
    SqliteDatabase,
    StreamingAgent,
    job_events,
)

HAIKU = Claude(model="claude-haiku-5-5", effort="low", thinking="off", max_output_tokens=1000)


class Topic(AgentInput):
    topic: str


class Note(AgentOutput):
    note: str | None = None


class Researcher(AgentBase):
    """The background job: it takes a while, and the chat does not wait for it."""

    name: str = "researcher"
    description: str = "Writes a short research note on a topic"
    version: str = "1"
    prompt: str = "Write a note of two or three sentences on the topic."
    model: ModelSettings = HAIKU
    input_model: ClassVar[type[AgentInput]] = Topic
    output_model: ClassVar[type[AgentOutput]] = Note


class Assistant(StreamingAgent):
    name: str = "assistant"
    description: str = "A chat that can start research in the background"
    version: str = "1"
    prompt: str = (
        "You chat in a sentence or two. When the user wants something researched, start the researcher in the background and carry on: "
        "its note arrives in a later message, and then you tell the user what it says."
    )
    model: ModelSettings = HAIKU
    background_jobs: list[type] = [Researcher]


class Session:
    """The conversation, and the wake-up. featherweight keeps neither: this is yours to copy and change."""

    def __init__(self, db: Database, runner: AgentRunner):
        self.db, self.runner = db, runner
        self.history: list[BaseMessage] = []
        self.turn = asyncio.Lock()  # one turn at a time
        self.watching: set[asyncio.Task] = set()

    async def say(self, message: BaseMessage) -> None:
        async with self.turn:
            run = self.runner.stream(Assistant, ChatInput.from_langchain([*self.history, message]))
            async for event in run:
                if isinstance(event, TextMessageContentEvent):
                    print(event.delta, end="", flush=True)
                elif isinstance(event, CustomEvent) and event.name == "background_started":
                    task = asyncio.create_task(self.wake(event.value["job_id"]))  # follow the child
                    self.watching.add(task)
                    task.add_done_callback(self.watching.discard)
            print()
            self.history += [message, *run.new_messages]

    async def wake(self, job_id: str) -> None:
        async for _ in job_events(self.db, job_id):  # until the job is final
            pass
        print("\n[the researcher is done]")
        await self.say(await self.runner.background_message(job_id))  # the next turn, with its result


async def read_line(prompt: str) -> str:
    """One line from the terminal, read by the event loop and not by a thread, so that Control-C can leave (a thread stuck in
    `input()` would keep the process waiting). An empty line, or the end of the input, is an empty string."""
    loop = asyncio.get_running_loop()
    line: asyncio.Future[str] = loop.create_future()
    print(prompt, end="", flush=True)
    loop.add_reader(sys.stdin, lambda: line.set_result(sys.stdin.readline().strip()))
    try:
        return await line
    finally:
        loop.remove_reader(sys.stdin)


async def main() -> None:
    async with (
        SqliteDatabase(":memory:") as db,
        AgentRunner(db) as runner,
    ):  # in memory: a restart loses what is pending
        session = Session(db, runner)
        while text := await read_line("\nyou> "):
            await session.say(HumanMessage(text))
        for task in session.watching:
            task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        pass
