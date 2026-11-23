# AGENTS.md

Instructions for coding agents. featherweight is an opinionated Python agent harness: an agent is a typed class, a runner runs it with
retries and limits, and jobs run in your process or on workers. Pydantic is the spec.

## Building with featherweight

- **Start with the book:** [`docs/book/README.md`](docs/book/README.md) builds one small project, a chapter at a time, from a first agent to
  workers. Its code is in [`docs/book/code/`](docs/book/code).
- **Use the words of the glossary:** [`docs/book/07-reference/03-the-glossary.md`](docs/book/07-reference/03-the-glossary.md) says what each term means and which name to use (sub-agent, background job, worker, run log, identity...). A test fails when the docs use a name it rules out.
- **Look things up in the guides:** [`docs/README.md`](docs/README.md) says which guide answers what. The specs in
  [`docs/specs/`](docs/specs/README.md) say what each part does and why.
- **A skill for coding agents** is in [`skills/featherweight/`](skills/featherweight/SKILL.md): a router by what you want to build, the rules below
  in full, a working app, and the book and the guides as references. Read it, or copy the folder into your tool's skills folder.

## Rules that save a round trip

- Import what you use: `from featherweight import AgentBase, AgentRunner`. Do not write `import featherweight as ftw`.
- An agent is a class: `input_model`, `output_model`, a `prompt`. Pass the class to the runner, not an instance: `runner.run(MyAgent, input)`.
- A failed run is a value. Check `result.status` (`RunStatus.SUCCESS` or `FAILED`); do not wrap `run` in `try`.
- Rules a schema cannot say go in `check_output`: raise to reject, and the runner retries.
- Capabilities are allow-lists: `tools`, `mcp_servers`, `skills`, `subagents` and `background_jobs` are what the developer gave the agent.
- The framework stores metadata only (status, tokens, timing, hashes). Results and inputs go to stores you provide.
- Who a run is for is an `identity`: a small model you define, declared with `identity_type` and passed as `identity=`, and read by a tool as `runtime.context.identity`. The model never sees it. The tool checks authorization.
- Never call a real model in tests: use a fake model (see [`docs/recipes/testing-with-a-fake-model.md`](docs/recipes/testing-with-a-fake-model.md)).
  A real call costs money: ask first, use the smallest model.

## Working on this repository

```
make hooks      # once: conventional commit messages, ruff, ty and the unit tests, on commit
make check      # ruff, format check and ty
make test       # the unit tests, with Postgres for the PostgresDatabase tests
```

- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org) (`feat(runner): ...`, `fix: ...`).
- Each change updates the docs, the specs and the skill with the code: the guides and the book are copied into the skill with
  `python3 scripts/build_skill.py`, and `python3 scripts/build_book.py` writes the book's navigation. The tests check both.
- A fact lives in one place: the spec that owns it. Other pages link to it. See [`docs/specs/README.md`](docs/specs/README.md).
- More: [`docs/development.md`](docs/development.md).
