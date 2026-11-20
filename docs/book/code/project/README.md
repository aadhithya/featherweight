# The finished project

The incident report assistant, all of it: a summariser with a tool, an MCP server, a skill and a sub-agent; a pipeline over every report; two workers on a
queue; a chat that starts a researcher in the background; and traces. It is the last chapter's code, and it runs in the container.

```
make run     # everything, with nothing to type (from docs/book/code/; from the repository root: make book-run)
make chat    # the interactive chat (make book-chat)
```

Put a key in `.env` first ([the container](../README.md)). The files, and which chapter taught each, are in
[the finished project](../../07-reference/04-the-finished-project.md).
