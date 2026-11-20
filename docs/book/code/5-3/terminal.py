"""One line from the terminal, read by the event loop and not by a thread, so that Control-C can leave."""

import asyncio
import sys


async def read_line(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    line: asyncio.Future[str] = loop.create_future()
    print(prompt, end="", flush=True)
    loop.add_reader(sys.stdin, lambda: line.set_result(sys.stdin.readline().strip()))
    try:
        return await line
    finally:
        loop.remove_reader(sys.stdin)
