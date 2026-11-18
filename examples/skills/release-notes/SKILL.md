---
name: release-notes
description: Turns a list of code changes or commits into user-facing release notes. Use when asked for release notes, a changelog, or a summary of what changed in a version.
license: Apache-2.0
metadata:
  author: micro
  version: "1.0"
---

# Release notes

1. Start with a line `## Version <number>`, using the version you were given.
2. Group the changes under `### New`, `### Improved` and `### Fixed`. Leave out a group that has no changes.
3. Write one line per change, starting with a verb, in terms of what the user sees or can now do. Leave out ticket
   numbers, file names and internal refactors that change nothing for the user.
4. Put the most important change first in its group.
