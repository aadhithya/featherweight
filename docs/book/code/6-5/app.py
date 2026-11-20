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
