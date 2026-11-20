"""Chapter 4-1: a chat that streams its answer. The report is the first message of the conversation."""

import asyncio
import sys
from pathlib import Path

from ag_ui.core import TextMessageContentEvent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from featherweight import AgentRunner, ChatInput, Gemini, ModelSettings, SqliteDatabase, StreamingAgent, default_db_path


class IncidentAssistant(StreamingAgent):  # a streaming agent answers in a chat: no output model, no retries
    name: str = "incident_assistant"
    description: str = "Answers questions about an incident report"
    version: str = "1"  # change it when the prompt or the behaviour changes: runs are told apart by it
    prompt: str = (
        "The first message is an incident report. Answer questions about it in a sentence or two, only from the report."
    )
    # the model is a setting of the agent
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")


async def read_line(prompt: str) -> str:  # the event loop reads the terminal, so that Control-C can leave
    loop = asyncio.get_running_loop()
    line: asyncio.Future[str] = loop.create_future()
    print(prompt, end="", flush=True)
    loop.add_reader(sys.stdin, lambda: line.set_result(sys.stdin.readline().strip()))
    try:
        return await line
    finally:
        loop.remove_reader(sys.stdin)


async def main(path: str = "reports/01-checkout-outage.txt") -> None:
    greeting = "I have the report. Ask me anything about it."
    history: list[BaseMessage] = [HumanMessage(Path(path).read_text()), AIMessage(greeting)]  # featherweight keeps none
    print(greeting)
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        while text := await read_line("\nyou> "):  # an empty line leaves
            user = HumanMessage(text)
            # the conversation is yours: the whole history goes in at each turn
            run = runner.stream(IncidentAssistant, ChatInput.from_langchain([*history, user]))
            async for event in run:
                if isinstance(event, TextMessageContentEvent):
                    print(event.delta, end="", flush=True)  # the answer, as the model writes it
            history += [user, *run.new_messages]  # what was said, for the next turn


if __name__ == "__main__":
    try:
        asyncio.run(main(*sys.argv[1:2]))
    except (KeyboardInterrupt, EOFError):
        pass
