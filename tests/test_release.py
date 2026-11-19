"""The release setup: the version is in step, and the workflow keeps the release pull request manual."""

import json
import tomllib
from pathlib import Path

import yaml

import featherweight

ROOT = Path(__file__).resolve().parent.parent


def test_the_package_version_is_the_version_in_pyproject():
    assert featherweight.__version__ == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_the_release_config_and_the_manifest_are_valid_and_agree():
    config = json.loads((ROOT / "release-please-config.json").read_text())
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert set(config["packages"]) == set(manifest) == {"."}
    assert config["packages"]["."]["extra-files"] == ["src/featherweight/__init__.py"]
    assert "x-release-please-version" in (ROOT / "src" / "featherweight" / "__init__.py").read_text()
    assert config["bump-minor-pre-major"] is True  # below 1.0, a breaking change bumps the minor version
    title = config[
        "pull-request-title-pattern"
    ]  # the release pull request, and so its commit, is a semantic bump commit
    assert title == "chore(release): bump version to ${version}"
    hidden = {section["type"] for section in config["changelog-sections"] if section.get("hidden")}
    assert "chore" in hidden  # so the bump commit itself is not a line of the changelog


def test_only_the_release_pull_request_is_manual_and_the_tag_is_made_when_it_is_merged():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))  # YAML reads a bare `on` as True
    assert set(triggers) == {"workflow_dispatch", "push"} and triggers["push"] == {"branches": ["main"]}
    by_hand, on_push = workflow["jobs"]["release-pr"], workflow["jobs"]["release"]
    assert by_hand["if"] == "github.event_name == 'workflow_dispatch'"
    assert by_hand["steps"][0]["with"]["skip-github-release"] is True  # by hand it opens the pull request, nothing more
    assert on_push["if"] == "github.event_name == 'push'"
    assert on_push["steps"][0]["with"]["skip-github-pull-request"] is True  # on a push it never opens one


# publishing to PyPI


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def test_publishing_follows_the_release_and_has_no_trigger_of_its_own():
    release = workflow("release.yml")
    triggers = release.get("on", release.get(True))
    assert set(triggers) == {"workflow_dispatch", "push"}  # publishing added no trigger
    publish = release["jobs"]["publish"]
    assert publish["needs"] == "release"  # it runs after the tag and the GitHub release are made...
    assert publish["if"] == "needs.release.outputs.release_created == 'true'"  # ...and only when they were just made
    assert publish["environment"] == "pypi"
    assert publish["permissions"] == {"contents": "write", "id-token": "write"}  # trusted publishing, no stored token
    uses = [step.get("uses", "") for step in publish["steps"]]
    assert any(u.startswith("pypa/gh-action-pypi-publish@") for u in uses)
    runs = " ".join(step.get("run", "") for step in publish["steps"])
    assert "python -m build" in runs and "twine check --strict" in runs
    assert publish["steps"][0]["with"]["ref"] == "${{ needs.release.outputs.tag_name }}"  # it builds the tag
    assert "PYPI_API_TOKEN" not in (ROOT / ".github" / "workflows" / "release.yml").read_text()


def test_the_dry_run_to_testpypi_is_by_hand_only_and_makes_no_release():
    dry = workflow("publish-testpypi.yml")
    assert set(dry.get("on", dry.get(True))) == {"workflow_dispatch"}
    job = dry["jobs"]["testpypi"]
    assert job["environment"] == "testpypi" and job["permissions"] == {"contents": "read", "id-token": "write"}
    publish = next(s for s in job["steps"] if s.get("uses", "").startswith("pypa/gh-action-pypi-publish@"))
    assert publish["with"]["repository-url"] == "https://test.pypi.org/legacy/"


def test_the_package_metadata_is_ready_for_pypi():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "featherweight" and project["license"] == "Apache-2.0"
    assert {"Typing :: Typed", "Development Status :: 3 - Alpha"} <= set(project["classifiers"])
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]  # a license expression replaces it
    assert {"Homepage", "Repository", "Issues", "Changelog", "Documentation"} <= set(project["urls"])
    assert all(url.startswith("https://github.com/aadhithya/featherweight") for url in project["urls"].values())
    assert (ROOT / "src" / "featherweight" / "py.typed").exists()


def test_the_readme_has_no_relative_links_because_pypi_shows_it_elsewhere():
    import re

    readme = (ROOT / "README.md").read_text()
    relative = [t for t in re.findall(r"\]\(([^)\s]+)\)", readme) if not t.startswith(("http://", "https://", "#"))]
    assert relative == []
    assert not re.search(r'<img[^>]+src="(?!https?://)', readme)
