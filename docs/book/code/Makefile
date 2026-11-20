.PHONY: build chapter run chat shell clean

# `make build INSTALL=pypi` installs featherweight from PyPI instead of from this repository's source.
INSTALL ?= source
ifeq ($(INSTALL),pypi)
export BOOK_CONTEXT := .
export BOOK_DOCKERFILE := Dockerfile
endif

RUN = docker compose run --rm --no-deps book

build:
	docker compose build

# Run a chapter's code: `make chapter N=1-2` runs 1-2/app.py. Needs a key in .env (see .env.example).
chapter:
	@test -n "$(N)" || { echo "usage: make chapter N=1-2"; exit 1; }
	$(RUN) python $(N)/app.py

# The finished project, with nothing to type.
run:
	$(RUN) python project/run.py

# The finished project's chat.
chat:
	$(RUN) python project/chat.py

shell:
	$(RUN) bash

# Remove the image and the data (the run log and the results). Your code and your .env stay.
clean:
	docker compose down --volumes --rmi local
