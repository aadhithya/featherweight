"""Copy the guides of `docs/` and the pages of the book into the skill, so that the skill reads from the same source as the docs.

    python scripts/build_skill.py          # write skills/featherweight/references/
    python scripts/build_skill.py --check  # fail if they are out of date (the tests run this)

The guides go to `references/<name>.md`, and the book (its home and every chapter in `docs/book/SUMMARY.md`) to `references/book/`,
without the breadcrumbs and the previous and next links, which mean nothing in the skill. `references/recipes.md` is written by hand
and is not touched. A link to another copied page stays relative, and every other relative link becomes a link into the repository on
GitHub, so that it works wherever the skill is installed.
"""

import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCES = ROOT / "skills" / "featherweight" / "references"
BOOK = ROOT / "docs" / "book"
GUIDES = [
    "agents.md",
    "models.md",
    "launching-agents.md",
    "mcp.md",
    "skills.md",
    "streaming.md",
    "persistence.md",
    "submitted-jobs.md",
    "background-agents.md",
    "production-stack.md",
    "observability.md",
    "example-pipeline.md",
]
REPO_URL = "https://github.com/aadhithya/featherweight/blob/main"
HEADER = "<!-- Copied from {source} by scripts/build_skill.py. Edit the original, not this file. -->\n\n"

LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
CHAPTER = re.compile(r"^- \[[^\]]+\]\((?P<path>[^)\s]+)\)\s*$")
NAV = re.compile(r"<!-- nav:top -->.*?<!-- /nav:top -->\n*|\n*<!-- nav:bottom -->.*?<!-- /nav:bottom -->\n*", re.S)


def copied() -> dict[str, str]:
    """Each source page (relative to the repository) and where its copy goes (relative to `references/`)."""
    pages = {f"docs/{name}": name for name in GUIDES}
    pages["docs/book/README.md"] = "book/README.md"
    for line in (BOOK / "SUMMARY.md").read_text().splitlines():
        if match := CHAPTER.match(line):
            pages[f"docs/book/{match['path']}"] = f"book/{match['path']}"
    return pages


def rewrite_links(text: str, source: str, pages: dict[str, str]) -> str:
    """Keep links between the copied pages, and point every other relative link at the repository. Code blocks are left alone."""
    here = posixpath.dirname(source)

    def fix(match: re.Match[str]) -> str:
        label, target = match.groups()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        path, _, anchor = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(here, path))
        suffix = "#" + anchor if anchor else ""
        if resolved in pages:
            return f"[{label}]({posixpath.relpath(pages[resolved], posixpath.dirname(pages[source])) + suffix})"
        return f"[{label}]({REPO_URL}/{resolved}{suffix})"

    parts = re.split(r"(^```.*?^```[^\n]*$)", text, flags=re.S | re.M)  # the odd parts are fenced blocks
    return "".join(part if n % 2 else LINK.sub(fix, part) for n, part in enumerate(parts))


def build() -> dict[str, str]:
    """The text of each copied page, by its path under `references/`."""
    pages = copied()
    built = {}
    for source, dest in pages.items():
        text = NAV.sub("", (ROOT / source).read_text()).strip("\n") + "\n"
        built[dest] = HEADER.format(source=source) + rewrite_links(text, source, pages)
    return built


def stale_copies(built: dict[str, str]) -> list[str]:
    """Copies of the book that no longer have a source."""
    book = REFERENCES / "book"
    return sorted(
        path.relative_to(REFERENCES).as_posix()
        for path in book.rglob("*.md")
        if path.relative_to(REFERENCES).as_posix() not in built
    )


def main(argv: list[str]) -> int:
    built = build()
    left = stale_copies(built)
    if "--check" in argv:
        stale = [
            name
            for name, text in built.items()
            if not (REFERENCES / name).exists() or (REFERENCES / name).read_text() != text
        ]
        if stale or left:
            print("out of date:", ", ".join([*stale, *left]), "- run: python scripts/build_skill.py")
            return 1
        return 0
    for name, text in built.items():
        (REFERENCES / name).parent.mkdir(parents=True, exist_ok=True)
        (REFERENCES / name).write_text(text)
    for name in left:
        (REFERENCES / name).unlink()
    print(f"wrote {len(built)} pages to {REFERENCES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
