"""Chapter 4-1, extra: attach a file to the chat. A message holds text, so the file's text goes into the message."""

import asyncio
from pathlib import Path

from ag_ui.core import TextMessageContentEvent
from app import IncidentAssistant
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from featherweight import AgentRunner, ChatInput, SqliteDatabase, default_db_path


def attach(question: str, path: str) -> HumanMessage:
    """A user message that carries a file as extra context. Reading and attaching files is yours: featherweight takes text only."""
    file = Path(path)
    return HumanMessage(f'{question}\n\n<attachment name="{file.name}">\n{file.read_text()}\n</attachment>')


async def main(report: str = "reports/01-checkout-outage.txt", notes: str = "4-1/runbook.md") -> None:
    greeting = AIMessage("I have the report. Ask me anything about it.")
    history: list[BaseMessage] = [HumanMessage(Path(report).read_text()), greeting]
    question = attach("Does the runbook say what to do about this?", notes)  # the question, with the file attached
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        async for event in runner.stream(IncidentAssistant, ChatInput.from_langchain([*history, question])):
            if isinstance(event, TextMessageContentEvent):
                print(event.delta, end="", flush=True)
    print()


if __name__ == "__main__":
    asyncio.run(main())
