"""The release setup: the version is in step, and the workflow keeps the release pull request manual."""

import json
import tomllib
from pathlib import Path

import yaml

import micro

ROOT = Path(__file__).resolve().parent.parent


def test_the_package_version_is_the_version_in_pyproject():
    assert micro.__version__ == tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_the_release_config_and_the_manifest_are_valid_and_agree():
    config = json.loads((ROOT / "release-please-config.json").read_text())
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert set(config["packages"]) == set(manifest) == {"."}
    assert config["packages"]["."]["extra-files"] == ["src/micro/__init__.py"]
    assert "x-release-please-version" in (ROOT / "src" / "micro" / "__init__.py").read_text()
    assert config["bump-minor-pre-major"] is True  # below 1.0, a breaking change bumps the minor version


def test_a_release_pull_request_is_opened_only_by_hand_and_a_release_is_made_only_on_a_push():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))  # YAML reads a bare `on` as True
    assert set(triggers) == {"workflow_dispatch", "push"} and triggers["push"] == {"branches": ["main"]}
    by_hand, on_push = workflow["jobs"]["release-pr"], workflow["jobs"]["release"]
    assert by_hand["if"] == "github.event_name == 'workflow_dispatch'"
    assert by_hand["steps"][0]["with"]["skip-github-release"] is True  # it opens the pull request, nothing more
    assert on_push["if"] == "github.event_name == 'push'"
    assert on_push["steps"][0]["with"]["skip-github-pull-request"] is True  # it never opens one on its own
