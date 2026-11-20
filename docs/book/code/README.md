# The book's code

The code of the book, one folder for each chapter, and the finished project. It runs **in a container**, so nothing is installed on your machine
and the agents, their tools and the MCP server all run inside it. Everything here is plain files: read them, and read the container's too.

- [What the container does](#what-the-container-does)
- [Run it](#run-it)
- [The layout](#the-layout)
- [Without Docker](#without-docker)
- [Reset, and install from PyPI](#reset-and-install-from-pypi)

## What the container does

| | |
|---|---|
| **Image** | `python:3.12-slim`, and `pip install 'featherweight[mcp,anthropic,otel]' opentelemetry-sdk` (the last two are for chapter 5-4, the traces). Nothing else. The [`Dockerfile.source`](Dockerfile.source) installs it from this repository's source (it is the default until featherweight is on PyPI), and the [`Dockerfile`](Dockerfile) from PyPI |
| **User** | `reader`, not root |
| **Mounted** | This folder, at `/code`, so that you can edit a chapter's file and run it again |
| **Data** | The sqlite file (the run log and the results) is `/data/book.db`, in a Docker volume. It survives between runs, and is not in this folder |
| **Environment** | Only `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `FTW_LOG_LEVEL`, read from your shell or from the `.env` in this folder. Nothing else reaches the container, and no key is in the image |
| **Network** | Open, because the agent calls the model's API. The container does not publish any port |
| **Started** | Only the `book` service: no database server. [`compose.yaml`](compose.yaml) is the whole setup |

## Run it

Set a key first: `cp .env.example .env`, and put the key of the model you use in `.env` (the book uses Gemini unless a chapter says otherwise).

```
make build                 # once: build the image
make chapter N=1-2         # run chapter 1-2's code: 1-2/app.py
make run                   # the finished project, with nothing to type: project/run.py
make chat                  # the finished project's chat: project/chat.py
make shell                 # a shell in the container
```

From the root of the repository, the same commands are `make book-build`, `make book-chapter N=1-2`, `make book-run`, `make book-chat` and
`make book-shell`. `make` is optional: the commands underneath are

```
docker compose build
docker compose run --rm --no-deps book python 1-2/app.py
docker compose run --rm --no-deps book python project/run.py
docker compose run --rm --no-deps book python project/chat.py
docker compose run --rm --no-deps book bash
```

Compose may print `No services to build` on each run: it is harmless. Set `FTW_LOG_LEVEL=INFO` to see what the runner does, line by line.

## The layout

```
Dockerfile, Dockerfile.source, compose.yaml, Makefile, .env.example   the container
reports/                  a few sample incident reports, which the project reads
services_server.py        the tiny MCP server (Part 2), which the agent starts inside the container
<part>-<chapter>/         the code of a chapter, for example 1-2/app.py
project/                  the finished project: run.py and chat.py
```

- **One folder for each chapter that has code**, named by its part and chapter (`1-2`, `2-1`, ...). Its entry point is `app.py`, and **each folder is
  complete and runs on its own**: it is the previous chapter's code, plus what the chapter adds. You can start from any of them.
- `project/` is the last chapter's code, with the two scripts: `run.py` runs everything, and `chat.py` is the interactive part.
- The files read the reports and `services_server.py` from this folder, which is the container's working directory.

## Without Docker

```
python -m venv .venv && . .venv/bin/activate
pip install 'featherweight[mcp,anthropic,otel]' opentelemetry-sdk   # until it is on PyPI: pip install '../../..[mcp,anthropic,otel]' from this folder
export GOOGLE_API_KEY=...                           # or ANTHROPIC_API_KEY
python 1-2/app.py
```

The same files run. This installs into the virtual environment, and the agents and the MCP server run on your machine.

## Reset, and install from PyPI

- **Reset** the data, the image and the volume: `make clean`. Your code and your `.env` stay.
- **Install from PyPI** when featherweight is on it: `make build INSTALL=pypi` builds from the [`Dockerfile`](Dockerfile) with this folder as its context. To make it the default, set
  `BOOK_CONTEXT` and `BOOK_DOCKERFILE` in `compose.yaml` to `.` and `Dockerfile`.
- **Another provider's extra** (`openai`): add it to the `pip install` line of the Dockerfile you use, and `make build` again.
