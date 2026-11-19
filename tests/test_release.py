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
