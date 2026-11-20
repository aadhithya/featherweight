"""Keep the navigation of the book (`docs/book/`) and its code listings in step with its one table of contents.

    python scripts/build_book.py          # write the breadcrumbs, the links, the contents and the code listings
    python scripts/build_book.py --check  # fail if any of them is out of date (CI and the commit hook run this)

`docs/book/SUMMARY.md` is the one table of contents: a `# Part` heading starts a part, and `- [Title](path)` is a chapter of it.
From it this script writes, between marker comments in each chapter: the breadcrumbs at the top, and the previous, up and next
links at the bottom; and, in `docs/book/README.md`, the contents. It also makes every fenced block that follows a
`<!-- code: code/1-2/app.py -->` comment equal that file (the path is relative to `docs/book`).

It fails when a chapter in the contents has no file, when a chapter file is not in the contents, and when a chapter's first
heading is not its title in the contents. A code listing whose file does not exist yet is skipped, with a note.
"""

import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "docs" / "book"
SUMMARY = BOOK / "SUMMARY.md"
HOME = "README.md"

CHAPTER = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<path>[^)\s]+)\)\s*$")
PART = re.compile(r"^# (?P<title>.+?)\s*$")
# Which version the docs are for: read from pyproject.toml on main by shields.io, so a release never has to edit the pages.
VERSION_BADGE = (
    "[![docs for](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Faadhithya%2Ffeatherweight%2Fmain%2F"
    "pyproject.toml&query=%24.project.version&label=docs%20for&color=blue)](https://pypi.org/project/featherweight/)"
)
CODE = re.compile(r"(?P<head><!-- code: (?P<path>\S+) -->\n+```(?P<lang>\w*)\n)(?P<body>.*?)\n```", re.S)
NAV_TOP = re.compile(r"<!-- nav:top -->.*?<!-- /nav:top -->\n*", re.S)
NAV_BOTTOM = re.compile(r"\n*<!-- nav:bottom -->.*?<!-- /nav:bottom -->\n*", re.S)
TOC = re.compile(r"<!-- toc -->.*?<!-- /toc -->", re.S)


def slug(heading: str) -> str:
    """The anchor GitHub makes from a heading."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).strip().replace(" ", "-")


def parse_summary() -> list[tuple[str, list[tuple[str, str]]]]:
    """The parts, in order, each with its chapters as (title, path relative to docs/book)."""
    parts: list[tuple[str, list[tuple[str, str]]]] = []
    for line in SUMMARY.read_text().splitlines():
        if (part := PART.match(line)) and part["title"] != "Summary":
            parts.append((part["title"], []))
        elif (chapter := CHAPTER.match(line)) and parts:
            parts[-1][1].append((chapter["title"], chapter["path"]))
    return parts


def link(frm: str, to: str, anchor: str = "") -> str:
    """A relative link from the file `frm` to the file `to` (both relative to docs/book)."""
    rel = posixpath.relpath(to, posixpath.dirname(frm) or ".")
    return rel + (f"#{anchor}" if anchor else "")


def top(path: str, part: str, title: str) -> str:
    crumbs = f"[Book]({link(path, HOME)}) › [{part}]({link(path, HOME, slug(part))}) › {title}"
    crumbs += f"  {VERSION_BADGE}"
    return f"<!-- nav:top -->\n{crumbs}\n<!-- /nav:top -->\n\n"


def bottom(path: str, part: str, before: tuple[str, str] | None, after: tuple[str, str] | None) -> str:
    previous = f"[← {before[0]}]({link(path, before[1])})" if before else f"[← Book]({link(path, HOME)})"
    up = f"[{part}]({link(path, HOME, slug(part))})"
    following = f" · [{after[0]} →]({link(path, after[1])})" if after else ""
    return f"\n\n<!-- nav:bottom -->\n---\n\n{previous} · {up}{following}\n<!-- /nav:bottom -->\n"


def contents(parts: list[tuple[str, list[tuple[str, str]]]]) -> str:
    lines = ["<!-- toc -->"]
    for title, chapters in parts:
        lines += ["", f"### {title}", ""]
        lines += [f"{n}. [{chapter}]({path})" for n, (chapter, path) in enumerate(chapters, 1)]
    return "\n".join([*lines, "", "<!-- /toc -->"])


def stamp(text: str, top_text: str, bottom_text: str) -> str:
    text = NAV_BOTTOM.sub("", NAV_TOP.sub("", text)).strip("\n")
    return top_text + text + bottom_text


def listings(text: str, path: str) -> str:
    """The text with each code listing made equal to its file."""

    def fix(match: re.Match[str]) -> str:
        file = BOOK / match["path"]
        if not file.exists():
            print(f"note: {path}: {match['path']} does not exist yet, the listing is not checked")
            return match.group(0)
        return match["head"] + file.read_text().rstrip("\n") + "\n```"

    return CODE.sub(fix, text)


def build() -> tuple[dict[Path, str], list[str]]:
    """The wanted text of each file the script writes, and the problems that no writing can mend."""
    parts = parse_summary()
    problems: list[str] = []
    wanted: dict[Path, str] = {}
    order = [(title, path) for _, chapters in parts for title, path in chapters]
    listed = {path for _, path in order}
    for found in sorted(BOOK.glob("[0-9][0-9]-*/*.md")):
        if found.relative_to(BOOK).as_posix() not in listed:
            problems.append(f"{found.relative_to(BOOK)} is not in SUMMARY.md")
    for part_title, chapters in parts:
        for title, path in chapters:
            file = BOOK / path
            if not file.exists():
                problems.append(f"{path} is in SUMMARY.md and does not exist")
                continue
            text = file.read_text()
            heading = re.search(r"^# (.+?)\s*$", text, re.M)
            if not heading or heading[1] != title:
                problems.append(f"{path}: the first heading must be '{title}' (as in SUMMARY.md)")
            if sum(1 for line in text.splitlines() if line.startswith("```")) % 2:
                problems.append(
                    f"{path}: a code fence is not closed (an odd number of ``` lines): the rest of the page would render as code"
                )
            index = [p for _, p in order].index(path)
            before = order[index - 1] if index else None
            after = order[index + 1] if index + 1 < len(order) else None
            wanted[file] = stamp(
                listings(text, path), top(path, part_title, title), bottom(path, part_title, before, after)
            )
    home = BOOK / HOME
    first = order[0] if order else None
    home_text = TOC.sub(lambda _: contents(parts), home.read_text())
    home_text = NAV_BOTTOM.sub("", home_text).rstrip("\n")
    wanted[home] = home_text + (
        f"\n\n<!-- nav:bottom -->\n---\n\n[Start: {first[0]} →]({first[1]})\n<!-- /nav:bottom -->\n" if first else "\n"
    )
    return wanted, problems


def main(argv: list[str]) -> int:
    wanted, problems = build()
    stale = [file for file, text in wanted.items() if file.read_text() != text]
    if "--check" in argv:
        for problem in problems:
            print(problem)
        if stale:
            print(
                "out of date:",
                ", ".join(str(f.relative_to(BOOK)) for f in stale),
                "- run: python scripts/build_book.py",
            )
        return 1 if problems or stale else 0
    for problem in problems:
        print(problem)
    for file, text in wanted.items():
        file.write_text(text)
    print(f"wrote the navigation of {len(wanted)} pages")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
