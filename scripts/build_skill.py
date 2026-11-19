"""Copy the guides of `docs/` into the skill, so that the skill reads from the same source as the docs.

    python scripts/build_skill.py          # write skills/featherweight/references/
    python scripts/build_skill.py --check  # fail if they are out of date (the tests run this)

`references/recipes.md` is written by hand and is not touched. A link to another copied guide stays relative, and every
other relative link becomes a link into the repository on GitHub, so that it works wherever the skill is installed.
"""

import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCES = ROOT / "skills" / "featherweight" / "references"
GUIDES = [
    "agents.md",
    "models.md",
    "launching-agents.md",
    "mcp.md",
    "skills.md",
    "streaming.md",
    "persistence.md",
    "example-pipeline.md",
]
REPO_URL = "https://github.com/aadhithya/featherweight/blob/main"
HEADER = "<!-- Copied from docs/{name} by scripts/build_skill.py. Edit the original, not this file. -->\n\n"

LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")


def rewrite_links(text: str) -> str:
    """Keep links between the copied guides, and point every other relative link at the repository."""

    def fix(match: re.Match[str]) -> str:
        label, target = match.groups()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        path, _, anchor = target.partition("#")
        if path in GUIDES:
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join("docs", path))
        return f"[{label}]({REPO_URL}/{resolved}{'#' + anchor if anchor else ''})"

    return LINK.sub(fix, text)


def build() -> dict[str, str]:
    """The text of each copied guide, by file name."""
    return {name: HEADER.format(name=name) + rewrite_links((ROOT / "docs" / name).read_text()) for name in GUIDES}


def main(argv: list[str]) -> int:
    built = build()
    if "--check" in argv:
        stale = [
            name
            for name, text in built.items()
            if not (REFERENCES / name).exists() or (REFERENCES / name).read_text() != text
        ]
        if stale:
            print("out of date:", ", ".join(stale), "- run: python scripts/build_skill.py")
            return 1
        return 0
    REFERENCES.mkdir(parents=True, exist_ok=True)
    for name, text in built.items():
        (REFERENCES / name).write_text(text)
    print(f"wrote {len(built)} guides to {REFERENCES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
