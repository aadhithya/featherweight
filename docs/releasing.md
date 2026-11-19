# Releasing

How a release is made. It is for the maintainers. `main` is protected, so a release goes through a **release pull
request**, and merging that pull request is the approval.

## Cut a release

Only the release pull request is a manual step. Merging it is how you choose when a release is made.

1. **Run the workflow.** In GitHub, Actions, `release`, Run workflow (or `gh workflow run release`). It reads the commit
   messages since the last release and opens, or updates, **one release pull request**. The pull request changes three
   things: the version in `pyproject.toml`, `__version__` in `src/micro/__init__.py`, and a new section at the top of
   [`CHANGELOG.md`](../CHANGELOG.md). Nothing is tagged or released yet.
2. **Review and merge it** when its checks are green. Read the version and the changelog section first.
3. **That is all.** The merge makes the tag `vX.Y.Z` and a GitHub release whose notes are the changelog section, by itself.
   A push to `main` that is not a merged release pull request does nothing.

No pull request is opened unless someone runs the workflow, and nothing is tagged or released until a release pull request
is merged.

## The bump commit

The release pull request, and so its commit, is a semantic commit: `chore(release): bump version to X.Y.Z`. It is a `chore`,
so it is not a line of the changelog.

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
| `.github/workflows/release.yml` | The workflow: by hand it opens the release pull request, and on a push to `main` it makes the tag and the GitHub release if that push is the merged release pull request |
| `release-please-config.json` | The release type, the pre-1.0 rule, the title of the bump commit, the changelog sections |
| `.release-please-manifest.json` | The last released version. The workflow keeps it up to date |
| `CHANGELOG.md` | The changelog. Written by the release pull request, not by hand |

## Not yet

Publishing to PyPI is not part of the release yet: it waits for the package name to be decided
([issue #24](https://github.com/aadhithya/micro/issues/24)). When it is built, it will be a job of this workflow that runs
after the tag is made, so merging the release pull request will publish the package too. Opening the release pull request
stays the only manual step.
