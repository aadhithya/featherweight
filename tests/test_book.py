"""The navigation tool of the book (`scripts/build_book.py`): it writes the breadcrumbs, the links, the contents and the code listings,
and its check fails when any of them is stale, a chapter is missing or unlisted, or a title differs."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def build_book():
    spec = importlib.util.spec_from_file_location("build_book", ROOT / "scripts" / "build_book.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def book(tmp_path, build_book, monkeypatch):
    """A small book: two parts, three chapters, and one chapter with a code listing."""
    root = tmp_path / "book"
    (root / "01-one").mkdir(parents=True)
    (root / "02-two").mkdir()
    (root / "code" / "1-1").mkdir(parents=True)
    (root / "SUMMARY.md").write_text(
        "# Summary\n\n# Part 1: One\n\n- [First](01-one/01-first.md)\n- [Second](01-one/02-second.md)\n\n# Part 2: Two\n\n- [Third](02-two/01-third.md)\n"
    )
    (root / "README.md").write_text("# The book\n\n<!-- toc -->\n<!-- /toc -->\n")
    (root / "01-one" / "01-first.md").write_text("# First\n\nText.\n")
    (root / "01-one" / "02-second.md").write_text("# Second\n\n<!-- code: code/1-1/app.py -->\n```python\nold\n```\n")
    (root / "02-two" / "01-third.md").write_text("# Third\n")
    (root / "code" / "1-1" / "app.py").write_text("print('hi')\n")
    monkeypatch.setattr(build_book, "BOOK", root)
    monkeypatch.setattr(build_book, "SUMMARY", root / "SUMMARY.md")
    return root


def test_the_book_in_the_repository_is_up_to_date(build_book):
    assert build_book.main(["--check"]) == 0, "run: python scripts/build_book.py"


def test_it_writes_breadcrumbs_links_contents_and_listings(build_book, book):
    assert build_book.main(["--check"]) == 1  # nothing is written yet
    assert build_book.main([]) == 0
    assert build_book.main(["--check"]) == 0
    second = (book / "01-one" / "02-second.md").read_text()
    assert second.startswith(
        "<!-- nav:top -->\n[Book](../README.md) › [Part 1: One](../README.md#part-1-one) › Second\n"
    )
    assert (
        "[← First](01-first.md) · [Part 1: One](../README.md#part-1-one) · [Third →](../02-two/01-third.md)" in second
    )
    assert "```python\nprint('hi')\n```" in second  # the listing is the file
    assert "[← Book](../README.md)" in (book / "01-one" / "01-first.md").read_text()
    third = (book / "02-two" / "01-third.md").read_text()
    assert (
        "[← Second](../01-one/02-second.md) · [Part 2: Two](../README.md#part-2-two)" in third
        and "→" not in third.split("nav:bottom")[1]
    )
    home = (book / "README.md").read_text()
    assert (
        "### Part 2: Two\n\n1. [Third](02-two/01-third.md)" in home and "[Start: First →](01-one/01-first.md)" in home
    )


def test_a_changed_code_file_makes_the_check_fail(build_book, book):
    build_book.main([])
    (book / "code" / "1-1" / "app.py").write_text("print('changed')\n")
    assert build_book.main(["--check"]) == 1
    build_book.main([])
    assert build_book.main(["--check"]) == 0 and "print('changed')" in (book / "01-one" / "02-second.md").read_text()


def test_a_listing_whose_file_does_not_exist_yet_is_skipped(build_book, book, capsys):
    (book / "code" / "1-1" / "app.py").unlink()
    build_book.main([])
    assert "does not exist yet" in capsys.readouterr().out
    assert build_book.main(["--check"]) == 0


def test_a_chapter_in_the_contents_without_a_file_fails(build_book, book):
    (book / "02-two" / "01-third.md").unlink()
    assert build_book.main(["--check"]) == 1


def test_a_chapter_file_that_is_not_in_the_contents_fails(build_book, book):
    (book / "02-two" / "02-extra.md").write_text("# Extra\n")
    assert build_book.main(["--check"]) == 1


def test_a_title_that_differs_from_the_contents_fails(build_book, book):
    build_book.main([])
    page = book / "02-two" / "01-third.md"
    page.write_text(page.read_text().replace("# Third", "# Another title"))
    assert build_book.main(["--check"]) == 1


def test_a_page_with_an_unclosed_code_fence_fails(build_book, book, capsys):
    (book / "02-two" / "01-third.md").write_text("# Third\n\n```text\noutput without a closing fence\n")
    assert build_book.main(["--check"]) == 1
    assert "code fence is not closed" in capsys.readouterr().out
