"""A chat in the terminal. Ctrl-C stops the answer, and you can correct the agent.

    python examples/chat/cli.py

Needs ANTHROPIC_API_KEY and the `anthropic` extra. Type an empty line to leave.
"""

import asyncio
import signal

from ag_ui.core import TextMessageContentEvent, ToolCallStartEvent
from langchain_core.messages import BaseMessage, HumanMessage
from support import Support

import featherweight as ftw


async def main() -> None:
    history: list[BaseMessage] = []  # featherweight keeps no conversation: you do
    async with ftw.Database(ftw.default_db_path()) as db, ftw.AgentRunner(db) as runner:
        while text := input("\nyou> ").strip():
            user = HumanMessage(text)
            run = runner.stream(Support, ftw.ChatInput.from_langchain([*history, user]))
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, run.cancel)  # Ctrl-C stops the answer
            async for event in run:
                if isinstance(event, TextMessageContentEvent):
                    print(event.delta, end="", flush=True)  # the answer, as it is written
                elif isinstance(event, ToolCallStartEvent):
                    print(f"\n  [{event.tool_call_name}]")
            asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
            history += [user, *run.new_messages]  # what the agent said and did, even if you stopped it


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        pass
