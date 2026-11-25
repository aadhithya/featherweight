"""Chapter 4-4: attach a file to a message. The file travels with the question, as inline data."""

import asyncio
import base64
import mimetypes
import sys
from pathlib import Path

from ag_ui.core import TextMessageContentEvent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from featherweight import AgentRunner, ChatInput, Gemini, ModelSettings, SqliteDatabase, StreamingAgent, default_db_path


class IncidentAssistant(StreamingAgent):
    name: str = "incident_assistant"
    description: str = "Answers questions about an incident report and the files attached to the chat"
    version: str = "2"  # the prompt changed
    prompt: str = (
        "The first message is an incident report. Answer questions about it in a sentence or two, "
        "from the report and from the files the user attaches."
    )
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    max_attachments = 3  # the limit is the agent's: ten by default, and 15 MiB in all


def attach(question: str, path: str) -> HumanMessage:
    """A message with a file: its bytes, base64-encoded, and its type. What a user may attach, and reading it, is yours."""
    file = Path(path)
    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    kind = mime.split("/")[0]
    block = {
        "type": kind if kind in ("image", "audio", "video") else "file",
        "base64": base64.b64encode(file.read_bytes()).decode(),
        "mime_type": mime,
        "filename": file.name,
    }
    return HumanMessage([{"type": "text", "text": question}, block])


async def main(report: str = "reports/01-checkout-outage.txt", runbook: str = "4-4/runbook.pdf") -> None:
    history: list[BaseMessage] = [HumanMessage(Path(report).read_text()), AIMessage("I have the report.")]
    questions = [
        attach("What does the runbook say to do about this?", runbook),
        HumanMessage("And who owns the integration?"),
    ]
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        for message in questions:  # the second question has no file: the first one is still in the history
            run = runner.stream(IncidentAssistant, ChatInput.from_langchain([*history, message]))
            async for event in run:
                if isinstance(event, TextMessageContentEvent):
                    print(event.delta, end="", flush=True)
            print()
            history += [message, *run.new_messages]


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
