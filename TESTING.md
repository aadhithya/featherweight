# Testing

How featherweight is tested, and the rules a change follows. This page owns these rules.

![What the tests cover](docs/assets/test-map.svg)

`make coverage` draws this map from the tests themselves, so it is not edited by hand.

![core coverage](docs/assets/badges/coverage-core.svg) ![tools coverage](docs/assets/badges/coverage-tools.svg)
![jobs coverage](docs/assets/badges/coverage-jobs.svg) ![chat coverage](docs/assets/badges/coverage-chat.svg)

## Commands

| Command | Runs |
|---|---|
| `make test-core` | Runner, agents, settings, identity, persistence, observability, package |
| `make test-tools` | Tools, MCP, discovery, skills, sub-agents |
| `make test-jobs` | Jobs, resume, results, workers, queues, background jobs |
| `make test-chat` | Streaming, adapters, attachments |
| `make test-patterns` | Recipes, book, examples, the skill and the docs rules |
| `make test` | Everything without a real model |
| `make coverage` | The same with coverage, the gate, the badges and the map |
| `make package-check` | Builds the package, installs it and imports it |
| `make e2e` | Real models, on every provider. It costs money, and is run by hand |

All but `make e2e` run with every API key and token blanked. `ARGS` is passed to pytest.

## The coverage gate

Decided on 2026-11-26.

- **95% overall, and 90% for each suite** that has source (core, tools, jobs, chat). Lines and branches count together.
- **Measured on one run of all the tests,** grouped by source file ([`scripts/test_report.py`](scripts/test_report.py)). A suite
  run alone would not count what the tests of another suite run.
- **The patterns have no percentage.** They have no source of their own: what counts is what their tests assert.
- **It runs on every pull request, and not in the commit hook,** so that commits are quick. In CI it is part of the `test`
  check, which `main` requires, so a pull request cannot be merged until it passes. It runs again for each new push to the
  pull request, and not for a push to `main`. No key is given to it, and the real-model tests are never run there.
  (Decided on 2026-11-26.)
- **The badges and the map are files in the repository.** The check fails when they are out of date. Run `make coverage`
  before you push a change that adds or removes tests, or moves coverage across a whole percent, and commit what it writes.
  A badge is red under 90%, and goes from near red at 90% to green at 100%. The map has the colours of the documentation
  site's dark theme.
- Code that only runs in a subprocess (the takeover test, some examples) is not counted.

Coverage says that a line ran, not that a test checked what it did. The rules below are for that.

## What a test must check

- **No real model.** A fake model stands in ([how](docs/recipes/testing-with-a-fake-model.md)). Everything else may be real:
  sqlite, Postgres, an MCP server in a subprocess, a worker process.
- **Check what the model was given,** not what the fake was told to answer. A fake follows its script whatever it is offered, so
  asserting its final answer proves little. Assert the input, the tool results and the history that it saw.
- **Check the promise of the page.** If a page says that content is not traced, that the history is kept or that a finished step is
  not paid for again, a test fails when that stops being true. Check what is refused as well as what works.
- **A new test should fail without the thing it tests.** Break it once and see.
- **Every recipe, every chapter folder with code, and every example is run.** Tests fail when a recipe or a chapter folder has
  none. That check sees that the code is run or read by its test, not what the test asserts.
- **A new test file goes into a suite,** and a new module into the coverage of one (`scripts/test_report.py`). A test fails
  until it does.

## Real models

One set of tests ([`tests/e2e`](tests/e2e)) runs on every provider, each on its smallest model
([the list](tests/e2e/e2e_providers.py)): Gemini, Claude and OpenAI. Only the model settings differ.

| Test | What it checks with the real provider |
|---|---|
| Tool and typed output, thinking off and low | The model calls a tool, and its typed output holds what the tool returned |
| Streamed chat | A tool call, and a second turn that sends back what the provider produced in the first |
| PDF attachment | The model reads a code that is only in the file |
| MCP, bound | It calls the tools of a server, and reads the picture and the document they return |
| MCP, found by search | It finds the tools and calls them, and they are never bound |
| Skill | It reads the skill, and the file that the skill points to |
| Sub-agent | It launches the child, and returns the child's summary unchanged |

- **Run by hand, before a release:** `make e2e`, with the keys in `.env`. It costs money. One provider:
  `make e2e ARGS="-k claude"`. A provider without a key is skipped, so read the summary for skips.
- **Bounded:** one attempt for each run, so that nothing is paid twice and a first failure shows; 2,000 output tokens for
  each model call; 20 steps for each agent. Each test prints what it used.
- **Assert what the framework did,** and values that the model can only have from a tool or a file, not its wording.
- **Offline too.** [`tests/test_e2e_offline.py`](tests/test_e2e_offline.py) runs the same tests on fake models in CI, with the
  network closed, and checks that each one fails on a wrong answer. A new real-model test needs a script in
  [`tests/e2e/e2e_fake.py`](tests/e2e/e2e_fake.py). This keeps the tests working; it says nothing about a provider.
- **Not covered by a real model:** jobs, resume, workers and background jobs, which depend little on the provider.

## The package

`make package-check` builds the package, installs it into new environments, without extras and with all of them, and imports
it, with the newest dependencies that it allows. Every pull request runs it.

## Audit of the pattern tests, 2026-11-26

The tests of the recipes, the book and the examples were read, and recipes were broken on purpose in a copy of the repository
to see whether their tests noticed (`doclearn` was left out).

- **Before:** 5 recipes had weak tests and 6 partial ones. Six deliberate breaks went unnoticed. Only 3 of the book's chapter
  folders were run, and the examples for skills, launching and MCP were only built.
- **After:** the 11 recipe tests were strengthened, and they caught all of 14 deliberate breaks. The breaks were chosen knowing
  the tests, so this shows that those gaps are closed, not that nothing can pass. Every chapter folder and every example (but
  `doclearn`) is run.
- **Found and fixed:** the session of [chapter 5-3](docs/book/code/5-3/app.py) and of
  [`examples/background`](examples/background/session.py) started a new turn for a background result that the running turn had
  already been given, so the model was told twice. Both now check the history first, as
  [the finished project](docs/book/code/project/chat.py) does.
