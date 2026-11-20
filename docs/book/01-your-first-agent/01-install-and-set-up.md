<!-- nav:top -->
[Book](../README.md) › [Part 1: Your first agent](../README.md#part-1-your-first-agent) › Install and set up
<!-- /nav:top -->

# Install and set up

Get featherweight running in a container, with a model key.

You need Docker, and a key for Gemini (the default model). Everything runs in a container, so nothing is installed on your machine.

```
cd docs/book/code
cp .env.example .env     # put your Gemini key in GOOGLE_API_KEY
make build               # once
make chapter N=1-1
```

<!-- code: code/1-1/app.py -->
```python
"""Chapter 1-1: is featherweight installed, and is there a key? No model is called."""

import os

from featherweight import __version__

print("featherweight", __version__)
for key in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    print(key, "is set" if os.environ.get(key) else "is not set")
```

```text
featherweight 0.1.0
GOOGLE_API_KEY is set
ANTHROPIC_API_KEY is not set
OPENAI_API_KEY is not set
```

**What to notice**

- No model was called: this only checks the install and the key. Only `GOOGLE_API_KEY` has to be set.
- The chapter code is mounted into the container, so you can edit a file and run it again.
- No Docker? [`code/README.md`](../code/README.md#without-docker) shows the same files in a virtual environment.

> **Why a container.** The agents, their tools and the MCP server of [Part 2](../02-abilities/02-mcp.md) all run inside it, and only the keys you
> list get in. What it does, line by line: [what the container does](../code/README.md#what-the-container-does).

Next: the agent.

<!-- nav:bottom -->
---

[← How to read this book](../00-start-here/04-how-to-read.md) · [Part 1: Your first agent](../README.md#part-1-your-first-agent) · [A typed agent →](02-a-typed-agent.md)
<!-- /nav:bottom -->
