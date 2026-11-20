"""Chapter 5-3: a chat that starts a researcher in the background, and a session that wakes it when the note is ready."""

import asyncio
import sys
from pathlib import Path

from ag_ui.core import CustomEvent, TextMessageContentEvent
from assistant import IncidentAssistant
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from terminal import read_line

from featherweight import AgentRunner, ChatInput, Database, SqliteDatabase, job_events


class Session:
    """The conversation, and the wake-up. featherweight keeps neither: this is yours to copy and change."""

    def __init__(self, db: Database, runner: AgentRunner, history: list[BaseMessage]):
        self.db, self.runner, self.history = db, runner, history
        self.turn = asyncio.Lock()  # one turn at a time
        self.watching: set[asyncio.Task] = set()

    async def say(self, message: BaseMessage) -> None:
        async with self.turn:
            run = self.runner.stream(IncidentAssistant, ChatInput.from_langchain([*self.history, message]))
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
        async for _ in job_events(self.db, job_id):  # until the child's job is final
            pass
        print("\n[the researcher is done]")
        await self.say(await self.runner.background_message(job_id))  # the next turn, with its result


async def main(path: str = "reports/01-checkout-outage.txt") -> None:
    report = HumanMessage(Path(path).read_text())
    async with (
        SqliteDatabase(":memory:") as db,
        AgentRunner(db) as runner,
    ):  # in memory: a restart loses what is pending
        session = Session(db, runner, [report, AIMessage("I have the report. Ask me anything about it.")])
        while text := await read_line("\nyou> "):  # try: what was the root cause?
            await session.say(HumanMessage(text))
        for task in session.watching:
            task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main(*sys.argv[1:2]))
    except (KeyboardInterrupt, EOFError):
        pass
