# Development

Issues and pull requests are welcome.

| Command | |
|---|---|
| `make build` | Build the image |
| `make test` | Unit tests (the model is mocked, no key needed); or `uv run pytest` |
| `make check` | Lint and format check (ruff) and types (ty); `make lint`, `make format`, `make typecheck` run one |
| `make hooks` | Install the git hooks: conventional commit messages, ruff, ty and the unit tests. CI only lints and type checks, so the tests run here, on commit |
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
skills/               the skill that teaches a coding agent to build with featherweight (references are copied from the guides and the book)
tests/                the harness's tests; tests/e2e uses the real model
```

`python3 scripts/build_skill.py` copies the guides and the book into the skill, and `python3 scripts/build_book.py` writes the book's navigation and checks its code listings ([the style](book/STYLE.md)). The tests check the skill, and the commit hook and CI check the book.

## Contributing

Run `make hooks` once. Commit messages follow [Conventional Commits](https://www.conventionalcommits.org) (`feat(runner): ...`,
`fix: ...`), and `make check` and `make test` must pass. Each change updates the docs, the specs and the skill with the code.
How a release is made: [Releasing](releasing.md).
