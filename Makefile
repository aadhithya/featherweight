.PHONY: build up down clean clean-all test lint format typecheck check hooks e2e pipeline example-skills example-launching example-mcp shell

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

# Remove the container and image. The sqlite files in the project directory (micro.db, e2e.db) are kept.
clean:
	docker compose down --rmi local --remove-orphans

# Same as clean, and also delete the sqlite files.
clean-all:
	docker compose down --rmi local --volumes --remove-orphans
	rm -f micro.db e2e.db

test:
	docker compose run --rm micro pytest

# Lint, formatting and types (ruff and ty, from uv.lock). `make check` runs all three without changing files.
lint:
	docker compose run --rm micro ruff check .

format:
	docker compose run --rm micro ruff format .

typecheck:
	docker compose run --rm micro ty check

check:
	docker compose run --rm micro sh -c "ruff check . && ruff format --check . && ty check"

# Install the git hooks (conventional commit messages, ruff, ty) in this clone. Run on the host, not in the container.
hooks:
	uv run pre-commit install

# Real Gemini calls (needs GOOGLE_API_KEY in .env). Narrow with e.g. `make e2e ARGS="-k claim"`.
e2e:
	docker compose run --rm micro pytest -m e2e -s -v $(ARGS)

# Learn, fill the form and generate (real Gemini calls). E.g. `make pipeline ARGS="examples/doclearn/samples/project_proposals --category project_proposals"`;
# see `make pipeline ARGS=--help`. Run folders go to ./runs.
pipeline:
	docker compose run --rm micro python -m doclearn.cli $(ARGS)

# An agent that uses skills (real Gemini calls). `make example-skills ARGS="your request"` for your own request.
example-skills:
	docker compose run --rm micro python examples/skills/agent.py $(ARGS)

# An agent that launches other agents (real Gemini calls). `make example-launching ARGS="your request"` for your own request.
example-launching:
	docker compose run --rm micro python examples/launching/agent.py $(ARGS)

# An agent that uses MCP tools: GitHub's MCP server (real Gemini and GitHub calls; needs GITHUB_TOKEN in .env, see examples/mcp).
# `make example-mcp ARGS="your question"` for your own question.
example-mcp:
	docker compose run --rm micro python examples/mcp/agent.py $(ARGS)

shell:
	docker compose exec micro bash
