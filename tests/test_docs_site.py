"""The documentation site: its config, the hooks that fill it, and the workflow that publishes it with each release."""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import docs_hooks  # noqa: E402


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def triggers(wf: dict) -> dict:
    return wf.get("on", wf.get(True))  # YAML reads a bare `on` as True


def test_the_reference_lists_the_modules_of_the_package_and_the_public_names_come_from_them():
    modules = docs_hooks.public_modules()
    assert {"runner", "models", "observers", "otel", "adapters.agui", "adapters.vercel", "sse"} <= set(modules)
    assert "adapters" not in modules and modules == sorted(modules)
    for module in modules:
        assert (ROOT / "src" / "featherweight" / f"{module.replace('.', '/')}.py").exists(), module


def test_every_page_of_the_guides_the_specs_and_the_recipes_is_in_the_navigation():
    nav = yaml.safe_load((ROOT / "mkdocs.yml").read_text().replace("!!python", ""))["nav"]
    listed = {path for entry in nav for value in entry.values() if isinstance(value, list) for path in map(str, value)}
    pages = {p.relative_to(ROOT / "docs").as_posix() for p in (ROOT / "docs").glob("*.md")}
    pages |= {p.relative_to(ROOT / "docs").as_posix() for p in (ROOT / "docs" / "specs").glob("*.md")}
    pages |= {p.relative_to(ROOT / "docs").as_posix() for p in (ROOT / "docs" / "recipes").glob("*.md")}
    pages = {"documentation.md" if page == "README.md" else page for page in pages}  # the README of docs/ is renamed
    assert pages <= listed, sorted(pages - listed)


def test_a_link_that_leaves_the_docs_goes_to_github_at_the_release_and_code_blocks_are_left_alone():
    class Config(dict):
        pass

    class Included:
        def __init__(self, src_uri):
            self.src_uri = src_uri
            self.inclusion = type("I", (), {"is_included": lambda self: True})()

    class Page:
        file = Included("agents.md")

    files = [Included("agents.md"), Included("runner.md")]
    config = Config(docs_dir=str(ROOT / "docs"))
    text = "[a](runner.md) [r](../README.md) [x](../src/featherweight/runner.py#L1) [web](https://x.dev)\n```\n[c](../README.md)\n```\n`[i](../README.md)`"
    out = docs_hooks.on_page_markdown(text, Page(), config, files)
    base = f"{docs_hooks.REPO}/blob/{docs_hooks.REF}"
    assert f"[r]({base}/README.md)" in out
    assert f"[x]({base}/src/featherweight/runner.py#L1)" in out
    assert "[a](runner.md)" in out and "[web](https://x.dev)" in out
    assert out.count("[c](../README.md)") == 1 and "`[i](../README.md)`" in out


def test_the_docs_are_published_after_the_package_and_by_hand_for_any_tag():
    release = workflow("release.yml")["jobs"]["docs"]
    assert release["needs"] == ["release", "publish"]  # after the package is out...
    assert release["if"] == "needs.release.outputs.release_created == 'true'"  # ...and only for a release just made
    assert release["uses"] == "./.github/workflows/docs.yml"
    assert release["with"] == {"tag": "${{ needs.release.outputs.tag_name }}"}
    assert release["permissions"] == {"contents": "write"}
    docs = workflow("docs.yml")
    assert set(triggers(docs)) == {"workflow_call", "workflow_dispatch"}  # no trigger of its own
    steps = docs["jobs"]["docs"]["steps"]
    assert steps[0]["with"]["ref"] == "${{ inputs.tag }}"  # the docs of the tag, not of main
    runs = " ".join(step.get("run", "") for step in steps)
    assert "mkdocs build --strict" in runs and "mike deploy --push --update-aliases" in runs
    assert "cut -d. -f1,2" in runs  # one version per minor release
    assert 'echo "$DOMAIN" > CNAME' in runs and docs["jobs"]["docs"]["env"]["DOMAIN"] == "ftw.asankar.de"


def test_the_site_is_built_strict_on_every_pull_request():
    steps = workflow("test.yml")["jobs"]["test"]["steps"]
    assert any(step.get("run") == "uv run mkdocs build --strict" for step in steps)
    assert yaml.safe_load((ROOT / "mkdocs.yml").read_text().replace("!!python", ""))["site_url"] == (
        "https://ftw.asankar.de/"
    )
