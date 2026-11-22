.PHONY: skill all-docs build up down clean clean-all test lint format typecheck check hooks e2e pipeline example-skills example-launching example-resume example-results example-jobs example-takeover example-observability example-chat example-chat-api example-background example-mcp book-build book-chapter book-run book-chat book-shell shell

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

# Remove the container and image. The sqlite files in the project directory (featherweight.db, e2e.db) are kept.
clean:
	docker compose down --rmi local --remove-orphans

# Same as clean, and also delete the sqlite files.
clean-all:
	docker compose down --rmi local --volumes --remove-orphans
	rm -f featherweight.db e2e.db

# Only this target starts the `postgres` service: the tests of PostgresDatabase run against it (FTW_TEST_POSTGRES_URL is set in compose.yaml).
# The others run with `--no-deps`, because nothing else uses Postgres.
test:
	docker compose run --rm featherweight pytest

# Lint, formatting and types (ruff and ty, from uv.lock). `make check` runs all three without changing files.
lint:
	docker compose run --rm --no-deps featherweight ruff check .

format:
	docker compose run --rm --no-deps featherweight ruff format .

typecheck:
	docker compose run --rm --no-deps featherweight ty check

check:
	docker compose run --rm --no-deps featherweight sh -c "ruff check . && ruff format --check . && ty check"

# Copy the guides of docs/ into the skill (skills/featherweight/references). The tests fail when they are out of date.
skill:
	python3 scripts/build_skill.py

# Put all the documentation in one file, ALL_DOCS.md at the root (git-ignored): the README, docs/ (guides, specs, book) and the examples' READMEs.
all-docs:
	python3 scripts/build_all_docs.py

# Install the git hooks (conventional commit messages, ruff, ty, unit tests) in this clone. Run on the host, not in the container.
hooks:
	uv run pre-commit install

# Real Gemini calls (needs GOOGLE_API_KEY in .env). Narrow with e.g. `make e2e ARGS="-k claim"`.
e2e:
	docker compose run --rm --no-deps featherweight pytest -m e2e -s -v $(ARGS)

# Learn, fill the form and generate (real Gemini calls). E.g. `make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"`;
# see `make pipeline ARGS=--help`. Run folders go to ./runs.
pipeline:
	docker compose run --rm --no-deps featherweight python -m doclearn.cli $(ARGS)

# An agent that uses skills (real Gemini calls). `make example-skills ARGS="your request"` for your own request.
example-skills:
	docker compose run --rm --no-deps featherweight python examples/skills/agent.py $(ARGS)

# An agent that launches other agents (real Gemini calls). `make example-launching ARGS="your request"` for your own request.
example-launching:
	docker compose run --rm --no-deps featherweight python examples/launching/agent.py $(ARGS)

# An agent that uses MCP tools: GitHub's MCP server (real Gemini and GitHub calls; needs GITHUB_TOKEN in .env, see examples/mcp).
# `make example-mcp ARGS="your question"` for your own question.
example-mcp:
	docker compose run --rm --no-deps featherweight python examples/mcp/agent.py $(ARGS)

# Resuming a job: a pipeline that crashes halfway and a second run that reuses the first half (real Claude Haiku calls; needs
# ANTHROPIC_API_KEY in .env, see examples/resume).
example-resume:
	docker compose run --rm --no-deps featherweight python examples/resume/agent.py $(ARGS)

# Writing a result store and checking it for conformance (no model, no key; see examples/results).
example-results:
	docker compose run --rm --no-deps featherweight python examples/results/store.py

# Submit a job and run a worker for it (no model, no key; see examples/jobs).
example-jobs:
	docker compose run --rm --no-deps featherweight python examples/jobs/run.py

# Kill a worker in the middle of a job and watch another take it over (no model, no key; see examples/jobs).
example-takeover:
	docker compose run --rm --no-deps featherweight python examples/jobs/takeover.py

# Run a job and print its trace (no model, no key; see examples/observability).
example-observability:
	docker compose run --rm --no-deps featherweight python examples/observability/trace.py

# A chat agent that starts a background agent and is woken when it is done (real Claude Haiku calls; needs ANTHROPIC_API_KEY, see examples/background).
example-background:
	docker compose run --rm --no-deps featherweight python examples/background/session.py

# The book's code runs in a container of its own (docs/book/code): `make book-build`, then `make book-chapter N=1-2`, `make book-run`, `make book-chat`.
book-build:
	$(MAKE) -C docs/book/code build

book-chapter:
	$(MAKE) -C docs/book/code chapter N=$(N)

book-run:
	$(MAKE) -C docs/book/code run

book-chat:
	$(MAKE) -C docs/book/code chat

book-shell:
	$(MAKE) -C docs/book/code shell

# A chat in the terminal with a streaming agent (real Claude Haiku calls; needs ANTHROPIC_API_KEY in .env, see examples/chat).
example-chat:
	docker compose run --rm --no-deps featherweight python examples/chat/cli.py

# The same agent behind FastAPI on http://localhost:8000: AG-UI at /agui, the Vercel AI SDK at /vercel.
example-chat-api:
	docker compose run --rm --no-deps -p 8000:8000 featherweight uvicorn --app-dir examples/chat api:app --host 0.0.0.0

shell:
	docker compose exec featherweight bash
