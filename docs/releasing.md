# Releasing

How a release is made. It is for the maintainers. `main` is protected, so a release goes through a **release pull
request**, and merging that pull request is the approval.

## Merging into main

- **A pull request is needed, and the `test` check must pass** on it, with every conversation resolved. This holds for
  maintainers too.
- **A contribution needs one approval.** A ruleset asks for it, and a repository admin may bypass that rule alone when
  merging a pull request: GitHub does not let anyone approve their own. The checks above are in the branch protection, which
  nobody bypasses.
- **Squash is the only way to merge.** The title of the pull request becomes the message of the commit on `main`, so **the
  title must be a Conventional Commit**: the version and the changelog are made from it. The `pr-title` check
  ([`pr-title.yml`](../.github/workflows/pr-title.yml)) tests its form, and runs again when the title is edited. It cannot
  tell whether the type is right, so read the title before you merge. Give a pull request that has a fix or a feature a `fix:` or `feat:` title, or keep it to one change.

## Cut a release

Only the release pull request is a manual step. Merging it is how you choose when a release is made.

1. **Run the workflow.** In GitHub, Actions, `release`, Run workflow (or `gh workflow run release`). It reads the commit
   messages since the last release and opens, or updates, **one release pull request**. The pull request changes the
   version in `pyproject.toml`, `__version__` in `src/featherweight/__init__.py`, `version` in `.claude-plugin/plugin.json` and the manifest,
   and adds a new section at the top of [`CHANGELOG.md`](../CHANGELOG.md). The docs and the book show a "docs for" badge that reads the version
   from `pyproject.toml` on `main`, so a release does not edit them. A new place that holds the version goes in `extra-files` of
   `release-please-config.json`. Nothing is tagged or released yet.
2. **Review and merge it** when its checks are green. Read the version and the changelog section first.
3. **That is all.** The merge makes the tag `vX.Y.Z` and a GitHub release whose notes are the changelog section, by itself,
   and then the `publish` job builds the package and publishes it to PyPI. The built files are attached to the release.
   Then the `docs` job publishes the documentation site for the release ([below](#the-documentation-site)).
   A push to `main` that is not a merged release pull request does nothing.

No pull request is opened unless someone runs the workflow, and nothing is tagged or released until a release pull request
is merged.

## The bump commit

The release pull request, and so its commit, is a semantic commit: `chore(release): bump version to X.Y.Z`. It is a `chore`,
so it is not a line of the changelog.

## How the version is chosen

From the commit messages on `main` (Conventional Commits), which are the titles of the merged pull requests, since the last release:

| Commit | Bumps |
|---|---|
| `fix:`, `perf:` | the patch version |
| `feat:` | the minor version |
| `feat!:`, or a `BREAKING CHANGE:` line in the body | the major version, **or the minor version below 1.0** |

Other types (`docs`, `refactor`, `test`, `ci`, `chore`, `build`) never start a release by themselves. `docs` and `refactor`
appear in the changelog when a release is made, and `test`, `ci`, `chore` and `build` do not. The changelog is written from
the commit messages, so write them as you want them read. Commits that are not in this form are ignored.

The first release was `0.1.0`: the manifest started at `0.0.0`, and its changelog lists everything since the repository was opened.

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
| `.github/workflows/docs.yml` | Publishes the documentation site for a release (called by `release.yml`), or for any tag, by hand |
| `mkdocs.yml`, `scripts/docs_hooks.py` | The site: its theme, the navigation, the API reference, the links to GitHub |
| `.github/workflows/publish-testpypi.yml` | A dry run of publishing, by hand, to TestPyPI |
| `CHANGELOG.md` | The changelog. Written by the release pull request, not by hand |

## The documentation site

The site is <https://ftw.asankar.de>: the book, the guides and the specs of `docs/`, and an API reference made from
the docstrings. It is built with MkDocs Material and mkdocstrings, and each release publishes its own version of it with
[mike](https://github.com/jimporter/mike), so there is nothing to do by hand.

- **Per release.** After `publish`, the `docs` job runs [`docs.yml`](../.github/workflows/docs.yml) on the release tag: it builds the site
  (strict, so a broken link stops it), and `mike deploy` adds it to the `gh-pages` branch. The links to code in the pages point at
  that tag on GitHub.
- **One version per minor release.** `v0.2.1` replaces the pages of `0.2`, and the alias `latest` points at them. The root of the
  site redirects to `latest`, and the version list is in the header. Below 1.0 a minor release is the breaking one, so this is
  where the pages should differ.
- **`gh-pages` is output.** One branch, a folder per version, committed by the workflow. Nobody edits it, and it needs no
  branch protection. The source is `main`.
- **Nothing is published between releases.** The docs on `main` are the docs of the next release.
- **A fix to the docs of a release already out:** Actions, `docs`, Run workflow, with the tag (`v0.2.1`). It builds from that tag, so
  to fix the pages of `0.2`, the fix must be in the tag you give.
- **Locally,** in the container: `make docs` serves it at <http://localhost:8001> (`make docs DOCS_PORT=9000` for another port), and `make docs-build` builds it into `site/` (git-ignored).
  Both the pull request check and the commit hook run the strict build.

**One-time setup, by the owner:**

1. After the first release (it creates `gh-pages`): Settings, Pages, Source: **Deploy from a branch**, `gh-pages`, `/ (root)`.
2. DNS for `asankar.de`: `CNAME ftw` → `aadhithya.github.io`. Then in Settings, Pages, set the custom domain
   `ftw.asankar.de` and tick **Enforce HTTPS** (GitHub issues the certificate). The workflow keeps the `CNAME` file in the
   branch, so a release does not remove the setting.
3. Optional: verify the domain in your account's Pages settings, so nobody else can claim the subdomain.
4. A longer name, such as `featherweight.asankar.de`, can only be a redirect to `https://ftw.asankar.de`: a Pages site has only one
   custom domain. Make it an HTTP redirect (URL forwarding) at the DNS provider, or a small repository of its own that publishes a
   page with a redirect.

## Publishing to PyPI

The `publish` job of the release workflow runs only after the tag and the release were just made (`needs: release`, on the
`release_created` output), so merging the release pull request is the decision to publish. It checks out the tag, builds the
sdist and the wheel (`python -m build`), runs `twine check --strict`, uploads with `pypa/gh-action-pypi-publish`, and attaches
the files to the GitHub release.

It uses **trusted publishing**: no PyPI token is stored in the repository. PyPI is told once that this repository, this
workflow and this environment may publish the project.

**One-time setup, by the owner** (it cannot be done from the repository):

1. On [pypi.org](https://pypi.org), Your projects, Publishing, **add a pending publisher** for the project `featherweight`:
   owner `aadhithya`, repository `featherweight`, workflow `release.yml`, environment `pypi`. (A pending publisher creates the
   project at the first upload.) Do the same on [test.pypi.org](https://test.pypi.org) with the workflow `publish-testpypi.yml`
   and the environment `testpypi`.
2. In the repository, Settings, Environments: `pypi` and `testpypi` are created the first time a workflow uses them. Add a
   required reviewer to `pypi` if you want a second gate before the upload; without one, the publish follows the merge.
3. The repository must be named `featherweight` on GitHub: the trusted publisher names it.

**A dry run.** Actions, `publish-testpypi`, Run workflow: it builds the package at the branch you pick and uploads it to
TestPyPI. Install it to look (`pip install -i https://test.pypi.org/simple/ featherweight`). It never makes a tag or a release.

**Check the build by hand,** at any time:

```
uv build && uvx twine check --strict dist/*
```
