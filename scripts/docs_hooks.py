"""MkDocs hooks for the documentation site (mkdocs.yml). The pages stay as they are on GitHub; the site is made from them.

- The navigation of the book is read from `docs/book/SUMMARY.md`, the one table of contents the book already has, so the
  site's menu and the book's own links cannot drift apart.
- One API reference page per public module is added to the site, from the list of modules in `featherweight/__init__.py`.
  A new module appears in the reference by itself.
- The home page is the README of the repository, so the site and the repository say the same thing about the project. The
  README of `docs/` becomes the "Documentation" page, and links to either README go to the right page of the site.
- A relative link that leaves the documentation (to the README, the examples, the tests, the source, the book's code, ...)
  is rewritten to the same file on GitHub, at the release being built (`FTW_DOCS_REF`, `main` when it is not set).
  Links between pages are left to MkDocs, which checks them (`--strict` fails on a broken one).
"""

import ast
import os
import posixpath
import re
import sys
from pathlib import Path

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files
from mkdocs.structure.pages import Page

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_book import parse_summary  # noqa: E402  (the same parser the book's own checks use)

REPO = "https://github.com/aadhithya/featherweight"
REF = os.environ.get("FTW_DOCS_REF") or "main"
PACKAGE = ROOT / "src" / "featherweight"
# Modules that are public but not listed in `_MODULES`: the adapters and the SSE helpers have their own import paths.
EXTRA_MODULES = ("adapters.agui", "adapters.vercel", "adapters.errors", "sse")

# A link to a README, which the site serves under another name: the README of the repository is the home page, and the README
# of docs/ is the Documentation page.
ALIASES = {"README.md": "documentation.md", "../README.md": "index.md"}
HOME_FRONT_MATTER = "---\ntitle: An opinionated agent harness\nhide:\n  - navigation\n  - toc\n---\n\n"
HOME_BUTTONS = (
    "\n\n[Build your first agent](book/README.md){ .md-button }\n"
    "[Read the documentation](documentation.md){ .md-button }\n"
    f"[View on GitHub]({REPO}){{ .md-button }}"
)
DOCS_ON_GITHUB = re.compile(re.escape(f"{REPO}/blob/main/docs/") + r"(?P<path>[^)#\s]+)")

FENCE = re.compile(r"(^```.*?^```|^~~~.*?^~~~)", re.S | re.M)
LINK = re.compile(r"(?<!\\)\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)")
INLINE_CODE = re.compile(r"`[^`\n]*`")


def public_modules() -> list[str]:
    """The modules of the reference, sorted: `_MODULES` of the package, the modules that define the public names
    (`_EXPORTS`), and the extra ones."""
    tree = ast.parse((PACKAGE / "__init__.py").read_text())
    names: set[str] = set(EXTRA_MODULES)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = {getattr(t, "id", "") for t in node.targets}
        if "_MODULES" in targets:
            names |= set(ast.literal_eval(node.value))
        if "_EXPORTS" in targets:
            names |= set(ast.literal_eval(node.value).values())
    return sorted(names - {"adapters"})


def api_page(module: str) -> str:
    return f"api/{module.replace('.', '-')}.md"


def api_source(module: str) -> str:
    return f"# `featherweight.{module}`\n\n::: featherweight.{module}\n"


def on_config(config: MkDocsConfig) -> MkDocsConfig:
    """Fill the book's and the reference's navigation, which are not written by hand in mkdocs.yml."""
    book: list[dict] = [{"Overview": "book/README.md"}]
    book += [{part: [{title: f"book/{path}"} for title, path in chapters]} for part, chapters in parse_summary()]
    reference = [{"Overview": "api/index.md"}] + [{f"featherweight.{m}": api_page(m)} for m in public_modules()]
    sections = {"Book": book, "API reference": reference}
    for entry in config["nav"]:
        for name in entry:
            if name in sections:
                entry[name] = sections[name]
    return config


def on_files(files: Files, config: MkDocsConfig) -> Files:
    """Add the generated pages of the API reference."""
    modules = public_modules()
    listing = "\n".join(f"- [`featherweight.{m}`]({posixpath.basename(api_page(m))})" for m in modules)
    index = (
        "# API reference\n\n"
        "Made from the docstrings and the type hints of the code, so it is always the code of this version. "
        "Most of the names are also available on the package itself (`from featherweight import AgentBase, AgentRunner`). "
        "The guides say how to use them, and the specs say what was decided and why.\n\n" + listing + "\n"
    )
    pages = {"api/index.md": index} | {api_page(m): api_source(m) for m in modules}
    # The README of docs/ moves to documentation.md, and the README of the repository is the home page.
    docs_readme = next((f for f in files if f.src_uri == "README.md"), None)
    if docs_readme is not None:
        pages["documentation.md"] = Path(docs_readme.abs_src_path).read_text()  # ty: ignore[invalid-argument-type]
        files.remove(docs_readme)
    pages["index.md"] = home_markdown((ROOT / "README.md").read_text())
    for src_uri, content in pages.items():
        files.append(File.generated(config, src_uri, content=content))
    return files


def home_markdown(readme: str) -> str:
    """The README as the home page: a wide page without the side menus, a row of buttons under the tagline, and the links
    to the docs on GitHub (which the README needs, because PyPI shows it elsewhere) pointing at the pages of the site."""
    tagline = re.search(r"^\*\*An opinionated agent harness[^\n]*\n", readme, re.M)
    if tagline:
        readme = readme[: tagline.end()].rstrip("\n") + HOME_BUTTONS + "\n" + readme[tagline.end() :]

    def to_site(match: re.Match) -> str:
        path = match["path"]
        return ALIASES.get(path, path)

    return HOME_FRONT_MATTER + DOCS_ON_GITHUB.sub(to_site, readme)


def on_pre_page(page: Page, config: MkDocsConfig, files: Files) -> Page:
    """The edit button of the two pages that are not files of docs/ goes to the file they are made from."""
    sources = {"index.md": "README.md", "documentation.md": "docs/README.md"}
    if page.file.src_uri in sources:
        page.edit_url = f"{REPO}/edit/main/{sources[page.file.src_uri]}"
    return page


def github_url(repo_path: str) -> str:
    kind = "tree" if (ROOT / repo_path).is_dir() else "blob"
    return f"{REPO}/{kind}/{REF}/{repo_path}"


ALERT = re.compile(r"^> \[!(?P<kind>WARNING|NOTE)\][ \t]*\n(?P<body>(?:^>.*(?:\n|$))+)", re.M)


def admonitions(markdown: str) -> str:
    """GitHub's alerts (`> [!WARNING]`) as the site's admonitions, so that a box is highlighted in both places."""

    def box(match: re.Match) -> str:
        lines = [re.sub(r"^> ?", "", line) for line in match["body"].rstrip("\n").split("\n")]
        indented = "\n".join(f"    {line}".rstrip() for line in lines)
        return f"!!! {match['kind'].lower()}\n\n{indented}\n"

    return ALERT.sub(box, markdown)


def on_page_markdown(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Show GitHub's alerts as boxes, and point the links that leave the documentation at GitHub."""
    markdown = admonitions(markdown)
    docs_dir = Path(config["docs_dir"]).resolve()
    docs_rel = docs_dir.relative_to(ROOT).as_posix()
    known = {f.src_uri for f in files if f.inclusion.is_included()}  # not the excluded ones: those go to GitHub
    page_dir = posixpath.dirname(page.file.src_uri)

    def rewrite(match: re.Match) -> str:
        target = match["target"]
        if re.match(r"^([a-z][a-z0-9+.-]*:|#|/)", target):  # a URL, an anchor, an absolute path
            return match[0]
        path, hash_, fragment = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(page_dir, path))
        alias = ALIASES.get(resolved)
        if alias and alias in known:
            return f"]({posixpath.relpath(alias, page_dir or '.')}{hash_}{fragment}{match['title'] or ''})"
        if resolved in known:
            return match[0]
        if f"{resolved}/README.md" in known:  # a link to a folder whose page is its README
            return f"]({path.rstrip('/')}/README.md{hash_}{fragment}{match['title'] or ''})"
        repo_path = posixpath.normpath(posixpath.join(docs_rel, resolved))
        if repo_path.startswith("..") or not (ROOT / repo_path).exists():
            return match[0]  # not a file of the repository: leave it, so that MkDocs reports it
        return f"]({github_url(repo_path)}{hash_}{fragment}{match['title'] or ''})"

    parts = FENCE.split(markdown)
    for i in range(0, len(parts), 2):  # the odd parts are fenced code, which is left alone
        pieces = INLINE_CODE.split(parts[i])
        codes = INLINE_CODE.findall(parts[i])
        pieces = [LINK.sub(rewrite, piece) for piece in pieces]
        parts[i] = "".join(p + (codes[j] if j < len(codes) else "") for j, p in enumerate(pieces))
    return "".join(parts)
