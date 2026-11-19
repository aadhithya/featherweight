.PHONY: skill build up down clean clean-all test lint format typecheck check hooks e2e pipeline example-skills example-launching example-resume example-results example-chat example-chat-api example-mcp shell

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

# Starts the `postgres` service too: the tests of PostgresDatabase run against it (FTW_TEST_POSTGRES_URL is set in compose.yaml).
test:
	docker compose run --rm featherweight pytest

# Lint, formatting and types (ruff and ty, from uv.lock). `make check` runs all three without changing files.
lint:
	docker compose run --rm featherweight ruff check .

format:
	docker compose run --rm featherweight ruff format .

typecheck:
	docker compose run --rm featherweight ty check

check:
	docker compose run --rm featherweight sh -c "ruff check . && ruff format --check . && ty check"

# Copy the guides of docs/ into the skill (skills/featherweight/references). The tests fail when they are out of date.
skill:
	python3 scripts/build_skill.py

# Install the git hooks (conventional commit messages, ruff, ty, unit tests) in this clone. Run on the host, not in the container.
hooks:
	uv run pre-commit install

# Real Gemini calls (needs GOOGLE_API_KEY in .env). Narrow with e.g. `make e2e ARGS="-k claim"`.
e2e:
	docker compose run --rm featherweight pytest -m e2e -s -v $(ARGS)

# Learn, fill the form and generate (real Gemini calls). E.g. `make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"`;
# see `make pipeline ARGS=--help`. Run folders go to ./runs.
pipeline:
	docker compose run --rm featherweight python -m doclearn.cli $(ARGS)

# An agent that uses skills (real Gemini calls). `make example-skills ARGS="your request"` for your own request.
example-skills:
	docker compose run --rm featherweight python examples/skills/agent.py $(ARGS)

# An agent that launches other agents (real Gemini calls). `make example-launching ARGS="your request"` for your own request.
example-launching:
	docker compose run --rm featherweight python examples/launching/agent.py $(ARGS)

# An agent that uses MCP tools: GitHub's MCP server (real Gemini and GitHub calls; needs GITHUB_TOKEN in .env, see examples/mcp).
# `make example-mcp ARGS="your question"` for your own question.
example-mcp:
	docker compose run --rm featherweight python examples/mcp/agent.py $(ARGS)

# Resuming a job: a pipeline that crashes halfway and a second run that reuses the first half (real Claude Haiku calls; needs
# ANTHROPIC_API_KEY in .env, see examples/resume).
example-resume:
	docker compose run --rm featherweight python examples/resume/agent.py $(ARGS)

# Writing a result store and checking it for conformance (no model, no key; see examples/results).
example-results:
	docker compose run --rm featherweight python examples/results/store.py

# A chat in the terminal with a streaming agent (real Claude Haiku calls; needs ANTHROPIC_API_KEY in .env, see examples/chat).
example-chat:
	docker compose run --rm featherweight python examples/chat/cli.py

# The same agent behind FastAPI on http://localhost:8000: AG-UI at /agui, the Vercel AI SDK at /vercel.
example-chat-api:
	docker compose run --rm -p 8000:8000 featherweight uvicorn --app-dir examples/chat api:app --host 0.0.0.0

shell:
	docker compose exec featherweight bash
