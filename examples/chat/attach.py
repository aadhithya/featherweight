"""Ask about a file: an image, a PDF, a recording or a video, sent with your question.

    python examples/chat/attach.py photo.png "What is in this picture?"

Needs ANTHROPIC_API_KEY and the `anthropic` extra, and a real Claude call is billed. featherweight only passes the file on, as
inline data: whether the model can read it is the provider's to say (Claude reads images and PDFs, and refuses audio and video).
"""

import asyncio
import base64
import mimetypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))  # so that this runs without installing anything

from ag_ui.core import (  # noqa: E402
    AudioInputContent,
    DocumentInputContent,
    ImageInputContent,
    InputContentDataSource,
    RunErrorEvent,
    TextInputContent,
    TextMessageContentEvent,
    UserMessage,
    VideoInputContent,
)

from featherweight import (  # noqa: E402
    AgentRunner,
    ChatInput,
    Claude,
    ModelSettings,
    SqliteDatabase,
    StreamingAgent,
    default_db_path,
)


class Reader(StreamingAgent):
    name: str = "reader"
    description: str = "Answers questions about a file that the user attaches"
    version: str = "1"
    prompt: str = "You answer questions about the file the user attaches. Answer in a few sentences."
    model: ModelSettings = Claude(model="claude-haiku-5-5", effort="low", thinking="off", max_output_tokens=1000)
    max_attachments = 1  # the application's limits: one file per question here (the default is 10, and 15 MB)


PARTS = {
    "image": ImageInputContent,
    "audio": AudioInputContent,
    "video": VideoInputContent,
}  # anything else is a document


def attachment(path: Path):
    """The file as a part of a message: its bytes, base64-encoded, with its mime type. A URL would be refused: you fetch, not us."""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    source = InputContentDataSource(value=base64.b64encode(path.read_bytes()).decode(), mime_type=mime)
    part = PARTS.get(mime.split("/")[0], DocumentInputContent)
    return (
        part(source=source, metadata={"filename": path.name}) if part is DocumentInputContent else part(source=source)
    )


async def main(path: Path, question: str) -> None:
    message = UserMessage(id="1", content=[TextInputContent(text=question), attachment(path)])
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        async for event in runner.stream(Reader, ChatInput(messages=[message])):
            if isinstance(event, TextMessageContentEvent):
                print(event.delta, end="", flush=True)
            elif isinstance(event, RunErrorEvent):
                print(f"\nThe provider said no: {event.message}")  # a failed turn is an event, not an exception
        print()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit('usage: python examples/chat/attach.py FILE "your question"')
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
