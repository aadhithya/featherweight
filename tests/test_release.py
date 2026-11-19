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


def test_nothing_runs_on_its_own_and_each_step_does_only_its_part():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))  # YAML reads a bare `on` as True
    assert set(triggers) == {"workflow_dispatch"}  # no push, no schedule: only the button
    assert triggers["workflow_dispatch"]["inputs"]["step"]["options"] == ["prepare", "publish"]
    prepare, publish = workflow["jobs"]["prepare"], workflow["jobs"]["publish"]
    assert prepare["if"] == "inputs.step == 'prepare'" and publish["if"] == "inputs.step == 'publish'"
    assert prepare["steps"][0]["with"]["skip-github-release"] is True  # it opens the pull request, nothing more
    assert publish["steps"][0]["with"]["skip-github-pull-request"] is True  # it tags and releases, nothing more
