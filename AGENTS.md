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
- A failed run is a value. Check `result.status` (`Status.SUCCESS` or `FAILED`); do not wrap `run` in `try`.
- Rules a schema cannot say go in `check_output`: raise to reject, and the runner retries.
- Capabilities are allow-lists: `tools`, `mcp_servers`, `skills`, `subagents` and `background_jobs` are what the developer gave the agent.
- Agents propose, your code executes. Give an agent no tool that does what needs a person's approval: it returns a typed proposal (or calls a `propose_*` tool that only writes one), your application shows it, and plain code acts after the approval. Only the streaming agent talks to the user; a sub-agent or a background job has its own read tools. A job that needs an answer ends with `open_questions` and is resumed with `resume_from`. See [`docs/specs/approvals.md`](docs/specs/approvals.md).
- The framework stores metadata only (status, tokens, timing, hashes). Results and inputs go to stores you provide.
- Who a run is for is an `identity`: a small model you define, declared with `identity_type` and passed as `identity=`, and read by a tool as `runtime.context.identity`. The model never sees it. The tool checks authorization.
- Never call a real model in tests: use a fake model (see [`docs/recipes/testing-with-a-fake-model.md`](docs/recipes/testing-with-a-fake-model.md)).
  A real call costs money: ask first, use the smallest model.

## Contributing to this repository

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before you change anything. For an agent, the rules that matter:

- **A person sends it.** Do not open an issue or a pull request, or answer a review, on your own. Prepare the change, and let the person read it and decide.
- **Spec first.** A change to behaviour starts as a change to the spec that owns it in [`docs/specs/`](docs/specs/README.md), with the decision and the reason. Then the code, the tests, the guides, the book and the skill. Do not invent behaviour, names or options: check them in the code.
- **Small and focused.** One change, no drive-by refactors.
- **Evidence.** Run `make check`, `make coverage` and `make docs-build`, report what they printed, and commit the files that `make coverage` writes. Tests use a fake model.
- **Say it.** The pull request template asks for the AI assistance used. Fill it in truthfully.

## Working on this repository

```
make hooks      # once: conventional commit messages, ruff, ty and the docs build, on commit (no tests: pull requests run them)
make check      # ruff, format check and ty
make test       # the unit tests, with Postgres for the PostgresDatabase tests
make coverage   # the same, with the coverage gate; run it before pushing, and commit the badges and test map it writes
```

- [`TESTING.md`](TESTING.md) has the rules for tests: the suites, the coverage gate, and what a test of a recipe, a chapter or an example must check.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org) (`feat(runner): ...`, `fix: ...`).
- Each change updates the docs, the specs and the skill with the code: the guides and the book are copied into the skill with
  `python3 scripts/build_skill.py`, and `python3 scripts/build_book.py` writes the book's navigation. The tests check both.
- A fact lives in one place: the spec that owns it. Other pages link to it. See [`docs/specs/README.md`](docs/specs/README.md).
- More: [`docs/development.md`](docs/development.md).
