# Development

Issues and pull requests are welcome.

| Command | |
|---|---|
| `make build` | Build the image |
| `make test` | Unit tests (the model is mocked, no key needed, and the keys and tokens are blanked); or `uv run pytest` |
| `make test-core`, `test-tools`, `test-jobs`, `test-chat`, `test-patterns` | One suite of the tests, e.g. `make test-jobs ARGS="-k lease"` |
| `make coverage` | All the tests with coverage: fails under 95% overall or 90% in a suite, and writes the badges and the test map ([the rules](https://github.com/aadhithya/featherweight/blob/main/TESTING.md)) |
| `make check` | Lint and format check (ruff) and types (ty); `make lint`, `make format`, `make typecheck` run one |
| `make hooks` | Install the git hooks: conventional commit messages, ruff, ty, and the tests with the coverage gate. CI only lints and type checks, so the tests run here, on commit |
| `make docs` | Serve the documentation site at http://localhost:8001 (`make docs DOCS_PORT=9000` for another port), in the container; `make docs-build` builds it (strict: a broken link fails). The commit hook and the pull request check run the build |
| `make e2e` | Tests against the real model (costs money) |
| `make pipeline ARGS="..."` | Run the example pipeline |
| `make example-skills` | Run the skills example agent |
| `make example-launching` | Run the example agent that launches agents |
| `make example-mcp` | Run the example agent that uses an MCP server |
| `make example-resume` | Run the example that resumes a failed job |
| `make example-jobs` | Run the example that submits a job to a worker |

The `PostgresDatabase` tests run when `FTW_TEST_POSTGRES_URL` is set (`make test` starts a Postgres with `docker compose` and sets it). Only `make test` starts it: the examples, `make check` and `make e2e` run with `--no-deps`.

## The repository

```
src/featherweight/    the harness
examples/             runnable examples, each with a README (launching, skills, mcp, resume, results, chat, jobs, doclearn)
docs/                 the guides; docs/specs has what each part does, and why
mkdocs.yml            the documentation site, made from docs/ and the docstrings (scripts/docs_hooks.py fills it in)
docs/recipes/         short patterns for one job each; the skill gets an abridged copy of them
skills/               the skill that teaches a coding agent to build with featherweight (references are copied from the guides and the book, and built from the recipes)
tests/                the harness's tests; tests/e2e uses the real model
```

The docs use the words of [the glossary](book/07-reference/03-the-glossary.md): `tests/test_docs_terms.py` fails when a page uses a name that the glossary rules out (say `sub-agent`, not `subagent`; `background job`, not `background agent`; `worker`, not `job worker`). `python3 scripts/build_skill.py` copies the guides and the book into the skill and builds its abridged recipes from `docs/recipes`, and `python3 scripts/build_book.py` writes the book's navigation and checks its code listings ([the style](book/STYLE.md)). The tests check the skill, and the commit hook and CI check the book.

## Contributing

Read [CONTRIBUTING.md](https://github.com/aadhithya/featherweight/blob/main/CONTRIBUTING.md) first: what a change needs, spec-first design, and the rules for changes made with AI tools. Run `make hooks` once. Commit messages follow [Conventional Commits](https://www.conventionalcommits.org) (`feat(runner): ...`,
`fix: ...`), and `make check` and `make test` must pass. Each change updates the docs, the specs and the skill with the code.
How a release is made: [Releasing](releasing.md).
