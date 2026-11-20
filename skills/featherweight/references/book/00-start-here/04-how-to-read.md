<!-- Copied from docs/book/00-start-here/04-how-to-read.md by scripts/build_skill.py. Edit the original, not this file. -->

# How to read this book

In order, or through [the map](02-the-map.md): go straight to what you want to build.

- **Short chapters.** Each is a goal, the code, how to run it, and what to notice. A "why" box links to the spec when a choice needs
  its reasons.
- **One small project.** You build an incident report assistant: a report goes in, a structured summary comes out. It gains one thing
  per chapter, so each chapter starts from the code of the one before.
- **Code you can run.** Every chapter's code is a file in [`code/`](https://github.com/aadhithya/featherweight/blob/main/docs/book/code/README.md), in a folder per chapter (`code/<part>-<chapter>/`).
  Each folder runs on its own, so you can start at any chapter. The last one is the finished project.
- **In a container.** The code runs in a container by default, so nothing touches your machine. [`code/README.md`](https://github.com/aadhithya/featherweight/blob/main/docs/book/code/README.md)
  says what the container does, how to start it, and how to run it without Docker.
- **A model key.** The default model is Gemini: set `GOOGLE_API_KEY` in `.env`. Other providers take their own key
  ([choosing a model](../01-your-first-agent/06-choosing-a-model.md)).

When you need an exact rule, the guides and the specs are the reference ([Part 7](../07-reference/01-the-guides.md)).
