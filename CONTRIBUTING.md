# Contributing to featherweight

Contributions are welcome: bug reports, fixes, docs, and features that fit. This page says what a change needs before a maintainer reads it.

featherweight is largely written with AI coding agents, so the rules are not about whether a tool was used. They are about whether a person has thought about the change, understands it, and answers for it. A pull request that nobody has thought about costs the maintainers more than it gives.

## Before you write code

1. **Open an issue first** for anything beyond a typo or a small fix, and wait for a maintainer to agree on the direction. A pull request without an agreed issue may be closed without a review, however good the code is.
2. **Read what the project is for.** [The philosophy](docs/book/00-start-here/03-philosophy.md) says what featherweight does and does not do. Most declined changes are not wrong, they are somewhere else: in your application, not in the harness.
3. **Design in the spec, then code.** featherweight is spec-driven. What each part does, why, and what is left out lives in [`docs/specs/`](docs/specs/README.md), one spec for each part. A change that adds or alters behaviour starts as a change to the spec that owns it, with the decision and the reason, and what you considered and rejected. The words come from [the glossary](docs/book/07-reference/03-the-glossary.md); [the units of work](docs/specs/units.md) say what run, attempt, execution and job mean.

A fact lives in one place: the spec that owns it. Other pages link to it.

## What a pull request contains

- **The spec**, when behaviour changes (see above), written so that a reader who has not seen your code can tell what the change is for.
- **The code**, small and focused: one change per pull request, no drive-by refactors or reformatting of what you did not need to touch.
- **Tests** that fail without the change. Use a fake model ([how](docs/recipes/testing-with-a-fake-model.md)). Never call a paid model API in a test. The tests under `tests/e2e` are run by a maintainer.
- **The docs**: the guide, the book chapter and the recipe that mention the behaviour, and the skill (`python3 scripts/build_skill.py`; `python3 scripts/build_book.py` for the book). The tests check that they are in step.
- **A title and commits in [Conventional Commits](https://www.conventionalcommits.org)** (`feat(runner): ...`, `fix: ...`), and a description that says why. Pull requests are squash merged, so the title is what the changelog reads.

Run `make hooks` once per clone. Then `make check` (ruff and ty), `make test` (the unit tests, with Postgres) and `make docs-build` (strict: a broken link fails) must pass on your machine before you open the pull request. They run in Docker, so you install nothing. A pull request also runs `make coverage` ([the rules](TESTING.md)): run it too, and commit the badges and the test map it writes. [Development](docs/development.md) lists the commands.

## AI-assisted contributions

Use whatever tools you like. These rules apply to the person who opens the pull request or the issue:

- **You are the author.** You have read every line, you can explain it, and you can change it when a reviewer asks. "The agent wrote it" is not an answer to a review comment.
- **Say so.** The pull request template asks whether AI tools were used, which, and for what. A line is enough. Autocomplete does not need a mention. Nobody is turned away for using a tool, and a change that hid it is treated as a more serious problem than the use.
- **Run it yourself.** Run the checks and the tests on your machine, and run the code. Output that an agent reports is not evidence.
- **No fabrication.** Do not cite APIs, options, issue numbers, benchmarks or behaviour you have not checked in this repository. A bug report states what you saw: the version, the steps, and the output.
- **No unattended agents.** Do not point an agent at the repository to open issues or pull requests, to reply to review comments, or to mass-produce changes. A person decides each time to send something, after reading it.
- **Review is a conversation with you.** Reply yourself, and keep to what the reviewer asked.

If you use an agent in your own checkout, [`AGENTS.md`](AGENTS.md) is written for it, and [the skill](skills/featherweight/SKILL.md) teaches it featherweight.

## What gets closed without a full review

A maintainer may close a pull request or an issue, with a short reason, when it:

- has no agreed issue behind it, or does not match the issue;
- changes behaviour without changing the spec, or does not follow the template;
- adds tests that pass whether the change is there or not, or that call a real model;
- is large, mixes several changes, or reformats and renames what it did not need to;
- invents behaviour, names or options that the code does not have, or documents what is not so;
- reads as unreviewed by its author: a description that does not match the diff, unanswered review questions, or answers that restate the question.

This is not a verdict on you. Open it again smaller, with the spec first.

## Reporting a bug or a vulnerability

A bug report needs the version (`pip show featherweight`), a minimal example that fails, and what you expected. Use a fake model if you can: a report that needs a paid key to reproduce is slower to fix. For a security issue, do not open a public issue: contact the maintainer privately through GitHub.

## Reviews and releases

A maintainer decides whether a change fits, and may ask for it to be smaller, to move into the spec, or to be dropped. Releases are made from the conventional commits ([Releasing](docs/releasing.md)), so the message of a commit is read by someone later: write it for them.

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
