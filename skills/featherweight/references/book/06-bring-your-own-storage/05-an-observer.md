<!-- Copied from docs/book/06-bring-your-own-storage/05-an-observer.md by scripts/build_skill.py. Edit the original, not this file. -->

# An observer

Watch what a run does, and send it where you want.

An observer is a class that the runner calls as things happen: a job starts, an attempt ends, a model call finishes. The observers are `OTelObserver` (we
ship it, [observability](../05-in-production/04-observability.md)) and yours. Every hook does nothing by default, so you override only the ones you need:

<!-- code: code/6-5/app.py -->
```python
"""Chapter 6-5: an observer of your own, checked with the kit. No model, no key."""

import asyncio

from featherweight import ModelCallEnd, Observer, testing


class Tokens(Observer):  # observers: OTelObserver (we ship it), or yours
    """Counts the tokens of every model call. featherweight stores nothing that an observer receives."""

    def __init__(self) -> None:
        self.total = 0

    async def model_call_finished(self, call: ModelCallEnd) -> None:
        self.total += (call.input_tokens or 0) + (call.output_tokens or 0)


async def main() -> None:
    await testing.check_observer(lambda: Tokens())  # raises if a rule is broken
    print("Tokens follows the Observer contract")  # use it: AgentRunner(db, observers=[Tokens()])


if __name__ == "__main__":
    asyncio.run(main())
```

```text
make book-chapter N=6-5
Tokens follows the Observer contract
```

Use it with `AgentRunner(db, observers=[Tokens()])`.

## What to notice

- An observer always gets metadata (ids, status, tokens, timing). It gets content (messages, inputs, outputs) only if you set `capture_content = True` on the class.
- An observer never changes a run: one that raises or is slow is logged and ignored. Calls are inline, so an observer that does slow work buffers for itself.

> **Why** content is opt-in: the framework stores metadata and nothing else, and what leaves the process is your decision. See [observers](https://github.com/aadhithya/featherweight/blob/main/docs/specs/observability.md#observers).
