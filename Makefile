.PHONY: build up down clean clean-all test e2e pipeline shell

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

# Learn, fill the form and generate (real Gemini calls). E.g. `make pipeline ARGS="examples/project_proposals --category project_proposals"`;
# see `make pipeline ARGS=--help`. Run folders go to ./runs.
pipeline:
	docker compose run --rm micro python -m doclearn.cli $(ARGS)

shell:
	docker compose exec micro bash
