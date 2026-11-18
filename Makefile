.PHONY: build up down clean clean-all test e2e pipeline example-skills example-launching shell

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

shell:
	docker compose exec micro bash
