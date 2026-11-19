# Releasing

How a release is made. It is for the maintainers. `main` is protected, so a release goes through a **release pull
request**, and merging that pull request is the approval.

## Cut a release

Nothing runs on its own. Both steps are a run of the `release` workflow by hand: in GitHub, Actions, `release`, Run
workflow, and choose the step (or `gh workflow run release -f step=prepare`).

1. **`prepare`.** The workflow reads the commit messages since the last release and opens, or updates, **one release pull
   request**. The pull request changes three things: the version in `pyproject.toml`, `__version__` in
   `src/micro/__init__.py`, and a new section at the top of [`CHANGELOG.md`](../CHANGELOG.md).
2. **Review and merge it** when its checks are green. Read the version and the changelog section first. This is the
   approval. Merging does not release anything.
3. **`publish`.** Run the workflow again with this step. It makes the tag `vX.Y.Z` and a GitHub release whose notes are
   the changelog section, from the merged release pull request. You can run it any time after the merge.

Nothing is tagged or released until `publish` is run, and no pull request is opened until `prepare` is run.

## How the version is chosen

From the commit messages (Conventional Commits, which the commit hook enforces), since the last release:

| Commit | Bumps |
|---|---|
| `fix:`, `perf:` | the patch version |
| `feat:` | the minor version |
| `feat!:`, or a `BREAKING CHANGE:` line in the body | the major version, **or the minor version below 1.0** |

Other types (`docs`, `refactor`, `test`, `ci`, `chore`, `build`) never start a release by themselves. `docs` and `refactor`
appear in the changelog when a release is made, and `test`, `ci`, `chore` and `build` do not. The changelog is written from
the commit messages, so write them as you want them read. Commits that are not in this form are ignored.

The first release is `0.1.0`: nothing has been released before, and the manifest starts at `0.0.0`. Its changelog lists
everything since the repository was opened.

## The token

A pull request made with the default `GITHUB_TOKEN` does not start other workflows, so the required `test` check would
never run on the release pull request, and it could not be merged. The workflow therefore uses the secret `RELEASE_TOKEN`:
a fine-grained personal access token for this repository, with **Contents** and **Pull requests** read and write. Create it
once in Settings, Secrets and variables, Actions.

Without the secret the workflow falls back to `GITHUB_TOKEN`. That works only if the setting "Allow GitHub Actions to create
and approve pull requests" (Settings, Actions, General) is on, and even then the release pull request's check does not
run, so the secret is the way to go.

## Files

| File | |
|---|---|
| `.github/workflows/release.yml` | The workflow, started by hand only: step `prepare` opens the release pull request, and step `publish` makes the tag and the GitHub release of the merged one |
| `release-please-config.json` | The release type, the pre-1.0 rule, the changelog sections |
| `.release-please-manifest.json` | The last released version. The workflow keeps it up to date |
| `CHANGELOG.md` | The changelog. Written by the release pull request, not by hand |

## Not yet

Publishing to PyPI is not part of the release yet: it waits for the package name to be decided
([issue #24](https://github.com/aadhithya/micro/issues/24)).
