"""The test suites, the coverage gate, the badges and the map of what is tested. TESTING.md has the rules.

    python scripts/test_report.py files <suite>                 # the test files of a suite (for `make test-<suite>`)
    python scripts/test_report.py report coverage.json ids.txt  # write the badges and the map, then apply the gate

`ids.txt` is what `pytest --collect-only -q` prints. `make coverage` runs both, and so does the commit hook.
"""

import colorsys
import json
import math
import sys
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).parent.parent
ASSETS = ROOT / "docs" / "assets"

OVERALL_MINIMUM = 95.0
SUITE_MINIMUM = 90.0
RED_UNDER = 90  # a badge under this is red; the colour is about the number, not about which gate applies to it

TITLES = {"core": "Core", "tools": "Tools", "jobs": "Jobs", "chat": "Chat", "patterns": "Patterns"}

# The dark theme of the documentation site (docs/assets/site.css, the `slate` scheme): the map is always dark, as the site
# is by default. A test keeps the two in step.
THEME = {
    "background": "#0c111b",
    "card": "#101b30",
    "border": "#233350",
    "text": "#d7deeb",
    "muted": "#aeb9cc",
    "accent": "#aac0e8",
}
SANS = 'ui-sans-serif, system-ui, -apple-system, "SF Pro Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif'
SERIF = '"Iowan Old Style", "Palatino Linotype", Palatino, Charter, "Book Antiqua", Georgia, serif'

# suite -> area of the map -> its test files, without `test_` and `.py`. Every test file is in exactly one place.
SUITES: dict[str, dict[str, str]] = {
    "core": {
        "Runner and agents": "runner models limits concurrency fingerprint",
        "Settings": "config model_settings",
        "Identity": "identity identity_hash identity_queue",
        "Persistence": "db postgres migrations",
        "Observability": "observers otel log log_lines",
        "Package and release": "package release report pr_title",
    },
    "tools": {
        "Tools and registry": "registry calc_date_tools",
        "MCP and discovery": "mcp discovery search",
        "Skills": "skills",
        "Sub-agents": "subagents",
    },
    "jobs": {
        "Jobs and results": "jobs job_class job_decorator job_timeout job_observers results resume",
        "Workers and queues": "worker takeover queue job_lease jobs_lookup",
        "Client and events": "client events",
        "Background jobs": "background_jobs background_events background_results",
    },
    "chat": {
        "Streaming": "streaming stream_events cancel messages",
        "Adapters": "agui vercel",
        "Attachments": (
            "attachments attachment_adapters attachment_http attachment_limits attachment_providers attachment_validation"
        ),
    },
    "patterns": {
        "Recipes": "recipes recipes_platform",
        "Book": "book book_code book_chapters",
        "Examples": "examples",
        "Skill and docs rules": "skill_package docs_site docs_terms migration_doc no_old_names",
    },
}

# suite -> the modules of src/featherweight that its coverage is measured on. Every module is in exactly one. The patterns
# have no source of their own: they are judged by what their tests assert.
SOURCES: dict[str, str] = {
    "core": (
        "__init__ runner models context errors config fingerprint model_settings identity db sqlite postgres migrations "
        "observers otel log testing"
    ),
    "tools": "tools registry search discovery mcp mcp_connections mcp_tools skills",
    "jobs": "jobs results queue worker client events sse background",
    "chat": "streaming stream_events messages adapters/__init__ adapters/agui adapters/vercel adapters/errors",
}


def test_files(suite: str) -> list[str]:
    return [f"tests/test_{name}.py" for names in SUITES[suite].values() for name in names.split()]


def counts_by_file(ids: str) -> Counter[str]:
    """How many tests each file has, from the node ids that pytest prints."""
    return Counter(line.split("::")[0] for line in ids.splitlines() if "::" in line)


def percentages(report: dict) -> tuple[float, dict[str, float]]:
    """The coverage (lines and branches together) of the whole package and of each suite's sources, from coverage.json."""
    files = {
        name.removeprefix("src/featherweight/").removesuffix(".py"): data for name, data in report["files"].items()
    }
    grouped = [module for modules in SOURCES.values() for module in modules.split()]
    if sorted(grouped) != sorted(files):
        missing, unknown = sorted(set(files) - set(grouped)), sorted(set(grouped) - set(files))
        raise SystemExit(f"SOURCES is out of date. In no suite: {missing}. Not measured: {unknown}")

    def percent(modules: list[str]) -> float:
        summaries = [files[module]["summary"] for module in modules]
        covered = sum(s["covered_lines"] + s.get("covered_branches", 0) for s in summaries)
        total = sum(s["num_statements"] + s.get("num_branches", 0) for s in summaries)
        return 100.0 * covered / total if total else 100.0

    return percent(grouped), {suite: percent(modules.split()) for suite, modules in SOURCES.items()}


def below_the_gate(overall: float, suites: dict[str, float]) -> list[str]:
    failures = []
    if overall < OVERALL_MINIMUM:
        failures.append(f"overall coverage is {overall:.2f}%, and the minimum is {OVERALL_MINIMUM:g}%")
    failures += [
        f"coverage of {suite} is {value:.2f}%, and the minimum for a suite is {SUITE_MINIMUM:g}%"
        for suite, value in suites.items()
        if value < SUITE_MINIMUM
    ]
    return failures


def shown_percent(value: float, before: int | None) -> int:
    """The whole percent a badge shows, rounded down. A value that moves a fraction of a point around a whole number keeps
    what the badge showed, so that a run which times a branch differently does not change the file."""
    if before is not None and before - 0.2 <= value < before + 1.2:
        return before
    return math.floor(value)


def badge_colour(percent: int) -> str:
    """Red under 90%. From 90% to 100% the hue goes from near red, through amber and olive, to a full green."""
    if percent < RED_UNDER:
        hue = 0.0
    else:
        hue = 12.0 + (min(percent, 100) - RED_UNDER) / (100 - RED_UNDER) * (135.0 - 12.0)
    red, green, blue = colorsys.hls_to_rgb(hue / 360, 0.35, 0.68)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def badge(label: str, percent: int) -> str:
    colour = badge_colour(percent)
    text = f"{percent}%"
    left, right = 10 + 7 * len(label), 16 + 7 * len(text)
    width = left + right
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img" aria-label="{escape(label)}: {text}" data-percent="{percent}">
  <title>{escape(label)}: {text}</title>
  <clipPath id="r"><rect width="{width}" height="20" rx="3"/></clipPath>
  <g clip-path="url(#r)"><rect width="{left}" height="20" fill="#4b5058"/><rect x="{left}" width="{right}" height="20" fill="{colour}"/></g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">
    <text x="{left / 2:g}" y="14">{escape(label)}</text><text x="{left + right / 2:g}" y="14">{text}</text>
  </g>
</svg>
"""


def percent_in(path: Path) -> int | None:
    if not path.exists():
        return None
    _, found, rest = path.read_text().partition('data-percent="')
    return int(rest.split('"')[0]) if found else None


def the_map(counts: Counter[str], shown: dict[str, int], overall: int) -> str:
    """One row for each suite: what it covers, how many tests each area has, and the coverage of its sources."""
    width, left, columns, column_width, row_height = 960, 24, 3, 222, 24
    name_width = 190
    parts: list[str] = []
    top = 76
    for suite, areas in SUITES.items():
        totals = {area: sum(counts[f"tests/test_{name}.py"] for name in names.split()) for area, names in areas.items()}
        rows = math.ceil(len(totals) / columns)
        height = max(rows * row_height + 24, 70)
        coverage = f"{shown[suite]}% covered" if suite in shown else "judged by assertions"
        parts.append(f'<line class="rule" x1="{left}" y1="{top}" x2="{width - left}" y2="{top}"/>')
        parts.append(f'<text class="suite" x="{left}" y="{top + 28}">{TITLES[suite]}</text>')
        parts.append(
            f'<text class="muted" x="{left}" y="{top + 47}">{sum(totals.values()):,} tests · {coverage}</text>'
        )
        for index, (area, total) in enumerate(totals.items()):
            x = left + name_width + (index % columns) * (column_width + 16)
            y = top + 14 + (index // columns) * row_height
            parts.append(f'<rect class="chip" x="{x}" y="{y}" width="{column_width}" height="20" rx="4"/>')
            parts.append(f'<text class="area" x="{x + 9}" y="{y + 14}">{escape(area)}</text>')
            parts.append(f'<text class="count" x="{x + column_width - 9}" y="{y + 14}">{total:,}</text>')
        top += height
    total = sum(counts[file] for suite in SUITES for file in test_files(suite))
    head = f"{total:,} tests without a real model · {overall}% of lines and branches covered"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{top + 12}" viewBox="0 0 {width} {top + 12}" role="img" aria-label="What featherweight's tests cover">
  <title>What featherweight's tests cover</title>
  <style>
    text {{ font-family: {SANS}; fill: {THEME["text"]}; }}
    .card {{ fill: {THEME["background"]}; stroke: {THEME["border"]}; }}
    .rule {{ stroke: {THEME["border"]}; }}
    .chip {{ fill: {THEME["card"]}; stroke: {THEME["border"]}; }}
    .head {{ font-family: {SERIF}; font-size: 21px; font-weight: 600; }}
    .suite {{ font-size: 15px; font-weight: 650; fill: {THEME["accent"]}; }}
    .area {{ font-size: 12px; }}
    .muted {{ font-size: 12px; fill: {THEME["muted"]}; }}
    .count {{ font-size: 12px; fill: {THEME["muted"]}; text-anchor: end; }}
  </style>
  <rect class="card" x="0.5" y="0.5" width="{width - 1}" height="{top + 11}" rx="8"/>
  <text class="head" x="{left}" y="34">What the tests cover</text>
  <text class="muted" x="{left}" y="56">{head}</text>
  {chr(10).join("  " + part for part in parts).strip()}
</svg>
"""


def write_if_changed(path: Path, content: str) -> None:
    if not path.exists() or path.read_text() != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"updated {path.relative_to(ROOT)}")


def report(coverage_json: str, ids_file: str) -> int:
    overall, suites = percentages(json.loads(Path(coverage_json).read_text()))
    counts = counts_by_file(Path(ids_file).read_text())

    shown: dict[str, int] = {}
    for suite, value in suites.items():
        path = ASSETS / "badges" / f"coverage-{suite}.svg"
        shown[suite] = shown_percent(value, percent_in(path))
        write_if_changed(path, badge(f"{suite} coverage", shown[suite]))
    path = ASSETS / "badges" / "coverage.svg"
    whole = shown_percent(overall, percent_in(path))
    write_if_changed(path, badge("coverage", whole))
    write_if_changed(ASSETS / "test-map.svg", the_map(counts, shown, whole))

    print(f"\ncoverage: overall {overall:.2f}% (minimum {OVERALL_MINIMUM:g}%)")
    for suite, value in suites.items():
        print(f"  {suite:<6} {value:.2f}% (minimum {SUITE_MINIMUM:g}%)")
    failures = below_the_gate(overall, suites)
    for failure in failures:
        print(f"FAILED: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["files", suite] if suite in SUITES:
            print(" ".join(test_files(suite)))
        case ["report", coverage_json, ids_file]:
            sys.exit(report(coverage_json, ids_file))
        case _:
            raise SystemExit(__doc__)
