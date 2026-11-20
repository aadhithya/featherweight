# The style of the book

One page, for whoever writes a chapter. Every chapter has the same shape and the same voice.

## A chapter

1. **Goal.** One sentence under the title: what the reader will have at the end.
2. **The code.** What to write, at most about 50 lines, shown whole.
3. **Run it.** The one command, and what the reader should see.
4. **What to notice.** A few bullets: the parts that matter, and nothing else.
5. **Why.** A box of two or three lines when a choice needs its reason, linking to the spec. Never retell the spec.
6. **Next.** One line to the next chapter (the tool writes the links; this is the thought).

A chapter is one or two screens. If it grows, split it. The first heading is the title in `SUMMARY.md`, exactly.

## The code

- Each chapter's code is a `.py` file in `docs/book/code/<part>-<chapter>/` (chapter 2 of part 1 is `code/1-2/`). Each folder is the whole
  project at that point and runs on its own.
- The markdown shows the same code. Put `<!-- code: code/1-2/app.py -->` on the line before a fenced block, and
  `python scripts/build_book.py` makes the block equal the file. The check fails if it differs. (ruff formats both the same way.)
- The code is checked by ruff and ty like all the code here, and written as a reader would write it: specific imports
  (`from featherweight import AgentBase, AgentRunner`), no path tricks. Check it by running it with fake models, not a real one.

## The voice

- Crisp. Say what to do and why it works, in as few words as it takes.
- No history, no stories, no "as we saw". The reader is building, not reading a novel.
- Write `featherweight` in lowercase, always. Say that the framework stores metadata and nothing else, and that you bring the storage.
- Say each fact once. Link to the guide or the spec for the rest, and do not copy them.

## Adding a chapter

Add its line to [`SUMMARY.md`](SUMMARY.md), create the file with the title as its only heading, and run `python scripts/build_book.py`. It writes the
breadcrumbs, the previous and next links and the contents. `--check` fails when any of them is stale, and runs in CI and in the commit hook. To
move a chapter, move its line.
