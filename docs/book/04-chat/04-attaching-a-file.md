<!-- nav:top -->
[Book](../README.md) › [Part 4: Chat](../README.md#part-4-chat) › Attaching a file  [![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2Fpyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)
<!-- /nav:top -->

# Attaching a file

Send the assistant a file with a question: a PDF, an image, a recording or a video.

<!-- code: code/4-4/app.py -->
```python
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
```

```
make book-chapter N=4-4
```

The assistant reads `4-4/runbook.pdf` with the first question, and answers the second from the conversation, with no file:

```
Roll back the last release of the payment form, tell the payments team, and post in the incident channel once payments succeed.
The payments team owns the integration with the payment provider.
```

**What to notice**

- **The file is a block of the message.** `attach` builds a `HumanMessage` whose content is a list: the question, then the file as `base64` with its `mime_type` and `filename`. A PDF is a `file` block; an image, audio or video is an `image`, `audio` or `video` block.
- **featherweight only passes it on.** It reads nothing, converts nothing and fetches nothing, and it does not know what the model can read. If the provider says no (a type it does not accept, a file that is too big), the turn fails with the provider's error, as any failed turn does.
- **Only the bytes are accepted, not a URL.** A message with a link in its file block is refused when the `ChatInput` is built. Download the file yourself, check it as you see fit, and send what you have. The bytes are base64 as `base64.b64encode` gives it (no `data:` prefix, no line breaks), and a file with no bytes is refused.
- **The file stays in the history.** The second question has no file, and it works because the first message, with its file, is still in `history`. Every turn sends the whole history again, so a file is sent again, and it counts again.
- **The limits are the agent's.** `max_attachments` is 10 by default and the data may not be over 15 MiB (`max_attachments = 0` turns attachments off). Here the agent says 3. Over a limit, `runner.stream()` raises an `AttachmentLimitError` before anything runs. It is a `BadRequest` (and a `ValueError`), so a web application answers it like one: [the handler of chapter 4-2](02-in-the-browser.md) gives it a 413.
- **Providers differ.** Gemini reads all four types, Claude reads images and PDFs, and a provider may drop a type without an error. Check [what the providers accept](../../attachments-providers.md), which is a dated snapshot, and try your type with your model.

> **Why bytes only.** How a file is fetched, scanned and stored is your application's decision, and a URL that a user chose is not one a framework should open for you. See
> [attachments](../../streaming.md#attachments) and [the spec](../../specs/attachments.md).

**Not a chat?** Attachments are a feature of the chat. An agent that a job runs, and a job that waits in a queue, do not carry files, on purpose: they pass a **reference** (an id for a file that you keep in your own store), and a tool fetches the file and checks who is asking. See [files for an agent or a job](../../recipes/files-for-an-agent-or-a-job.md).

Next: Part 5 puts the assistant on workers, with background research and traces.

<!-- nav:bottom -->
---

[← Stopping and the conversation](03-stopping-and-the-conversation.md) · [Part 4: Chat](../README.md#part-4-chat) · [Submit and a worker →](../05-in-production/01-submit-and-a-worker.md)
<!-- /nav:bottom -->
