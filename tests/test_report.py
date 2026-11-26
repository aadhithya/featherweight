"""The test suites, the coverage gate and the commit hook that runs them (`scripts/test_report.py`, TESTING.md)."""

import colorsys
import importlib.util
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent


def load():
    spec = importlib.util.spec_from_file_location("report_script", ROOT / "scripts" / "test_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load()


def test_every_test_file_is_in_exactly_one_suite():
    listed = [path for suite in report.SUITES for path in report.test_files(suite)]
    there = sorted(f"tests/{path.name}" for path in (ROOT / "tests").glob("test_*.py"))
    assert sorted(listed) == there  # a new file goes into SUITES, once


def test_every_module_of_the_package_is_in_the_coverage_of_exactly_one_suite():
    package = ROOT / "src" / "featherweight"
    there = sorted(path.relative_to(package).with_suffix("").as_posix() for path in package.rglob("*.py"))
    assert sorted(m for modules in report.SOURCES.values() for m in modules.split()) == there


def test_the_gate_needs_95_overall_and_90_in_each_suite():
    suites = {"core": 99.0, "tools": 90.0}
    assert report.below_the_gate(95.0, suites) == []
    [overall] = report.below_the_gate(94.99, suites)
    assert "overall" in overall
    [one] = report.below_the_gate(99.0, {**suites, "tools": 89.99})
    assert "tools" in one


def coverage_of(covered: int, module_covered: dict[str, int] | None = None) -> dict:
    """A coverage.json in which each module has 100 lines, `covered` of them run, unless the module says otherwise."""
    modules = [m for names in report.SOURCES.values() for m in names.split()]
    return {
        "files": {
            f"src/featherweight/{m}.py": {
                "summary": {"covered_lines": (module_covered or {}).get(m, covered), "num_statements": 100}
            }
            for m in modules
        }
    }


def test_a_suite_is_measured_on_its_own_modules():
    overall, suites = report.percentages(coverage_of(100, {"streaming": 0}))
    assert suites["core"] == 100 and suites["chat"] < 90 < overall


def test_a_module_in_no_suite_stops_the_report():
    data = coverage_of(100)
    data["files"]["src/featherweight/new_thing.py"] = {"summary": {"covered_lines": 1, "num_statements": 1}}
    with pytest.raises(SystemExit, match="new_thing"):
        report.percentages(data)


def test_a_badge_rounds_down_and_does_not_flap_around_a_whole_number():
    assert report.shown_percent(94.99, None) == 94  # never more than was measured
    assert report.shown_percent(94.9, 95) == 95 and report.shown_percent(95.1, 94) == 94
    assert report.shown_percent(94.7, 95) == 94 and report.shown_percent(96.3, 94) == 96


def hue_of(colour: str) -> float:
    red, green, blue = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(red, green, blue)[0] * 360


def test_a_badge_is_red_under_90_and_gets_greener_up_to_100():
    assert {report.badge_colour(p) for p in (0, 50, 89)} == {report.badge_colour(89)} and hue_of(
        report.badge_colour(89)
    ) == 0
    hues = [hue_of(report.badge_colour(p)) for p in range(90, 101)]
    assert hues == sorted(hues) and len(set(hues)) == 11  # each percent is a step towards green
    assert hues[0] < 20 and 125 < hues[-1] < 145  # near red at 90, green at 100
    assert report.badge_colour(97) in report.badge("coverage", 97)


def test_the_map_has_the_colours_of_the_documentation_sites_dark_theme():
    css = (ROOT / "docs" / "assets" / "site.css").read_text()
    slate = css.split('[data-md-color-scheme="slate"] {')[1].split("}")[0]
    drawn = report.the_map(Counter(), {suite: 95 for suite in report.SOURCES}, 96)
    for colour in report.THEME.values():
        assert colour in slate and colour in drawn
    assert "prefers-color-scheme" not in drawn  # dark for everyone


@pytest.mark.parametrize(
    "changed",
    [
        "docs/recipes/a-pipeline-of-two-agents.md",
        "docs/book/code/5-3/app.py",
        "docs/getting-started.md",
        "skills/featherweight/SKILL.md",
        "scripts/test_report.py",
        "examples/skills/agent.py",
        "src/featherweight/runner.py",
        "conftest.py",
        "Makefile",
    ],
)
def test_the_commit_hook_runs_the_tests_for_a_change_to(changed):
    """The pattern tests run the recipes and the book, so a change to a page must run them."""
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    [hook] = [h for repo in config["repos"] for h in repo["hooks"] if h["id"] == "coverage"]
    assert hook["entry"] == "make coverage" and re.search(hook["files"], changed)


def test_the_tests_and_the_coverage_run_with_every_key_blanked():
    makefile = (ROOT / "Makefile").read_text()
    blanked = re.search(r"^NO_KEYS = (.*)$", makefile, re.M).group(1)
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"):
        assert f"-e {name}= " in blanked + " "
    commands = [line for line in makefile.replace("\\\n", " ").splitlines() if line.startswith("\t")]
    run_tests = [line for line in commands if re.search(r"pytest|coverage run|\$\(TEST_RUN\)", line)]
    [real] = [line for line in run_tests if "-m e2e" in line]
    blanked_runs = [line for line in run_tests if line != real]
    assert len(blanked_runs) == 3  # `test`, the suites and `coverage`
    assert all("$(NO_KEYS)" in line or "$(TEST_RUN)" in line for line in blanked_runs)
    assert re.search(r"^TEST_RUN = .*\$\(NO_KEYS\)", makefile, re.M)
