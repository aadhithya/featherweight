"""Check that the title of a pull request, in `PR_TITLE`, is a Conventional Commit (docs/releasing.md).

    PR_TITLE="fix(worker): stop cleanly" python3 scripts/check_pr_title.py

It checks the form only: whether `test:` or `fix:` is the right type is for the person who merges to see.
"""

import os
import re
import sys

# the types that the commit hook accepts (conventional-pre-commit)
TYPES = ("build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test")
TITLE = re.compile(rf"({'|'.join(TYPES)})(\([^()\r\n]+\))?!?: \S[^\r\n]*")


def is_conventional(title: str) -> bool:
    return TITLE.fullmatch(title) is not None


def main() -> int:
    title = os.environ.get("PR_TITLE", "")
    if is_conventional(title):
        print("The title is a Conventional Commit.")
        return 0
    print(f"Not a Conventional Commit: {title!r}")  # quoted, so that it cannot be read as a command of the runner's log
    print("Write it as `type(scope): subject`, for example `feat(runner): launch agents` or `fix: handle empty input`.")
    print(f"The scope is optional, `!` marks a breaking change, and the types are: {', '.join(TYPES)}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
