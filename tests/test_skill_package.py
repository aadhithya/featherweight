"""The skill in `skills/featherweight`: it loads as a skill, its guides match the docs, and its code runs (on a fake model)."""

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import Scripted, structured_reply

from featherweight.model_settings import Gemini
from featherweight.skills import Skill

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "featherweight"


def python_blocks(path: Path) -> list[str]:
    return re.findall(r"```python\n(.*?)```", path.read_text(), re.S)


def test_the_skill_loads_with_our_own_skill_model():
    skill = Skill.from_directory(SKILL)
    assert skill.name == "featherweight"
    assert "featherweight" in skill.description and "Use when" in skill.description
    assert len(skill.instructions.splitlines()) < 500  # the specification's advice
    assert {"references/agents.md", "references/models.md", "references/recipes.md"} <= set(skill.resources)


def test_every_reference_the_skill_points_to_exists():
    body = (SKILL / "SKILL.md").read_text()
    named = set(re.findall(r"`(references/[\w\-/]+\.md)`", body))  # the guides, and the chapters of the book
    assert named and all((SKILL / path).is_file() for path in named)


def test_the_copied_guides_match_the_docs():
    spec = importlib.util.spec_from_file_location("build_skill", ROOT / "scripts" / "build_skill.py")
    assert spec and spec.loader
    build_skill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_skill)
    assert build_skill.main(["--check"]) == 0, "run: python scripts/build_skill.py"


RECIPES = ROOT / "docs" / "recipes"
RECIPE_PAGES = sorted(p for p in RECIPES.glob("*.md") if p.name != "README.md")


@pytest.mark.parametrize(
    "path", [SKILL / "SKILL.md", SKILL / "references" / "recipes.md", *RECIPE_PAGES], ids=lambda p: p.name
)
def test_all_python_in_the_skill_and_the_recipes_is_valid(path):
    blocks = python_blocks(path)
    assert blocks
    for block in blocks:
        ast.parse(block)


def test_the_working_app_in_the_skill_runs_on_a_fake_model(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    fake = Scripted(script=[structured_reply("ExtractOutput", {"people": ["Anna Berger", "Tom Okafor"]})])
    monkeypatch.setattr(Gemini, "build", lambda self: fake)

    definition, pipeline, *_ = python_blocks(SKILL / "SKILL.md")
    namespace: dict = {"__name__": "skill_example"}
    exec(definition, namespace)
    exec(pipeline, namespace)  # defines main() and runs it

    assert "['Anna Berger', 'Tom Okafor']" in capsys.readouterr().out
    assert (tmp_path / "featherweight.db").exists()


def test_the_recipes_pipeline_defines_and_the_fake_model_test_file_passes_as_written(tmp_path):
    definition, pipeline, *_ = python_blocks(SKILL / "SKILL.md")
    namespace: dict = {"__name__": "skill_example"}
    exec(definition, namespace)
    exec(pipeline.replace("asyncio.run(main())", ""), namespace)  # the imports and main(), not run
    exec(python_blocks(RECIPES / "a-pipeline-of-two-agents.md")[0], namespace)  # the pipeline of two agents: defines it

    # The recipe's test file, saved next to the agent as `app.py`, as the recipe says, and run by pytest.
    (tmp_path / "app.py").write_text(definition)
    (tmp_path / "test_app.py").write_text(
        next(b for b in python_blocks(RECIPES / "testing-with-a-fake-model.md") if "class FakeModel" in b)
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "asyncio_mode=auto",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "4 passed" in done.stdout


def test_the_recipes_index_lists_every_page_and_the_skill_has_them_all():
    index = (RECIPES / "README.md").read_text()
    listed = re.findall(r"^\| \[[^\]]+\]\(([^)\s]+\.md)\)", index, re.M)
    assert sorted(listed) == [p.name for p in RECIPE_PAGES]  # a page that is not in the index would not reach the skill
    skill = (SKILL / "references" / "recipes.md").read_text()
    for page in RECIPE_PAGES:
        title = page.read_text().splitlines()[0].removeprefix("# ")
        assert f"## {title}\n" in skill and f"/docs/recipes/{page.name})" in skill
    assert "skill:omit" not in skill


def test_what_a_recipe_marks_to_omit_is_left_out_of_the_skill(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("build_skill", ROOT / "scripts" / "build_skill.py")
    assert spec and spec.loader
    build_skill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_skill)
    page = tmp_path / "docs" / "recipes" / "x.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "# X\n\nShort.\n\n<!-- skill:omit -->\nLong explanation.\n<!-- /skill:omit -->\n\n```python\nx = 1\n```\n"
    )
    monkeypatch.setattr(build_skill, "ROOT", tmp_path)
    text = build_skill.abridged("x.md", {})
    assert text.startswith("## X\n") and "Short." in text and "x = 1" in text
    assert "Long explanation" not in text and "skill:omit" not in text
    assert text.endswith("/docs/recipes/x.md)\n")
