"""MkDocs hooks for the documentation site (mkdocs.yml). The pages stay as they are on GitHub; the site is made from them.

- The navigation of the book is read from `docs/book/SUMMARY.md`, the one table of contents the book already has, so the
  site's menu and the book's own links cannot drift apart.
- One API reference page per public module is added to the site, from the list of modules in `featherweight/__init__.py`.
  A new module appears in the reference by itself.
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
    for src_uri, content in pages.items():
        files.append(File.generated(config, src_uri, content=content))
    return files


def github_url(repo_path: str) -> str:
    kind = "tree" if (ROOT / repo_path).is_dir() else "blob"
    return f"{REPO}/{kind}/{REF}/{repo_path}"


def on_page_markdown(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Point the links that leave the documentation at GitHub."""
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
