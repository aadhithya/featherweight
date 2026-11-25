# Approving what an agent proposes

An agent never books, pays or sends: it proposes, a person approves, and plain code executes. The agent below searches flights and returns a typed proposal, and it has no tool that books. A function of yours does that, after the approval.

```python
import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from langchain_core.tools import tool
from pydantic import BaseModel

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    ModelSettings,
    SqliteDatabase,
    default_db_path,
)

# yours: the flight search, your database of proposals and bookings, and who may approve
FLIGHTS = {
    ("MUC", "LIS", "2026-12-12"): [
        {"id": "f1", "airline": "TAP", "departs": "07:10", "price_eur": 142},
        {"id": "f2", "airline": "Lufthansa", "departs": "11:45", "price_eur": 189},
    ]
}
PROPOSALS: dict[str, dict] = {}
BOOKINGS: dict[str, dict] = {}  # by idempotency key
APPROVERS = {"u1"}


class Search(AgentInput):
    origin: str
    destination: str
    day: str


class Option(BaseModel):
    id: str
    airline: str
    departs: str
    price_eur: int


class Proposal(AgentOutput):  # what the agent returns: options to choose from, not a booking
    options: list[Option] = []
    recommended: str = ""  # the id of one option


@tool
def search_flights(origin: str, destination: str, day: str) -> list[dict]:
    """Search the flights of a day. It only reads."""
    return FLIGHTS.get((origin, destination, day), [])


class FlightFinder(AgentBase):
    name: str = "flight_finder"
    description: str = "Finds flights and proposes the best options"
    version: str = "0.1.0"
    prompt: str = "Search the flights for the trip and propose the best options. You cannot book: someone else does, after the person approves."
    model: ModelSettings = Gemini()
    tools: list = [search_flights]  # read-only: there is no tool that books
    input_model: ClassVar[type[AgentInput]] = Search
    output_model: ClassVar[type[AgentOutput]] = Proposal


def fingerprint(proposal: Proposal) -> str:
    return hashlib.sha256(json.dumps(proposal.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def keep(proposal: Proposal, minutes: int = 30) -> dict:
    """Stage one is done: keep the proposal, with its hash and an expiry, and show it to a person."""
    record = {
        "id": uuid.uuid4().hex[:8],
        "proposal": proposal,
        "hash": fingerprint(proposal),  # what the person was shown
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=minutes),
        "status": "pending",
    }
    PROPOSALS[record["id"]] = record
    return record


class NotApproved(Exception):
    pass


def approve(
    proposal_id: str, option_id: str, shown_hash: str, approver: str, idempotency_key: str, now: datetime | None = None
) -> dict:
    """Stage two, the executor: plain code, no model. It checks everything, then books once."""
    if idempotency_key in BOOKINGS:
        return BOOKINGS[idempotency_key]  # the same approval again changes nothing
    record = PROPOSALS.get(proposal_id)
    if record is None or record["status"] != "pending":
        raise NotApproved("there is no pending proposal with that id")
    if (now or datetime.now(timezone.utc)) > record["expires_at"]:
        record["status"] = "expired"
        raise NotApproved("the proposal has expired")
    if shown_hash != record["hash"]:
        raise NotApproved("the proposal is not the one that was shown")
    if (
        approver not in APPROVERS
    ):  # the approver's identity, from your authentication: never anything that the model said
        raise NotApproved("this person may not approve")
    option = next((o for o in record["proposal"].options if o.id == option_id), None)
    if option is None:
        raise NotApproved("that is not an option of the proposal")
    if not any(f["id"] == option.id for flights in FLIGHTS.values() for f in flights):  # what can have changed since
        raise NotApproved("that flight is no longer available")
    booking = {
        "booking_id": uuid.uuid4().hex[:8],
        "flight": option.model_dump(),
        "approved_by": approver,
    }  # an API call, in real life
    record["status"] = "approved"
    BOOKINGS[idempotency_key] = booking
    return booking


async def main() -> None:
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        result = await runner.run(FlightFinder, Search(origin="MUC", destination="LIS", day="2026-12-12"))
    record = keep(result.output)
    print("proposed", [o.id for o in record["proposal"].options], "- nothing is booked, a person has to approve")

    # Draw the screen from the typed proposal, wait as long as it takes, and when the person approves:
    booking = approve(record["id"], "f1", record["hash"], approver="u1", idempotency_key="approval-1")
    again = approve(record["id"], "f1", record["hash"], approver="u1", idempotency_key="approval-1")
    print(
        "booked",
        booking["flight"]["airline"],
        "| the same approval again is the same booking:",
        again == booking,
        "| bookings:",
        len(BOOKINGS),
    )


if __name__ == "__main__":
    asyncio.run(main())
```

- **The agent cannot act.** Its allow-list has a search and no booking, so a wrong answer, or a prompt injection, can only make a wrong proposal.
- **An approval is for exactly what was shown.** The person's client sends the proposal id, the option and the hash it was shown, never the arguments of the action, and the executor refuses a proposal that changed, expired or is not pending.
- **The approver is the person, not the model.** `approve` checks who they are with your own authentication, and an idempotency key makes a second approval change nothing.
- **Draw the screen yourself** from the fields of the proposal. A sentence that the model wrote about it is decoration.

To propose in the middle of a conversation, give the agent a tool that writes a proposal and cannot act. The chat shows a card, your code executes after the yes, and the outcome goes into the conversation for the next turn:

```python
class Reply(AgentOutput):
    reply: str = ""


@tool
def propose_booking(option_id: str) -> str:
    """Propose booking a flight for the person to approve. This does not book."""
    flights = [f for flights in FLIGHTS.values() for f in flights if f["id"] == option_id]
    if not flights:
        return "There is no such flight."
    record = keep(Proposal(options=[Option(**flights[0])], recommended=option_id))
    return f"Proposed as {record['id']}. It is not booked: the person has to approve it."


class Assistant(AgentBase):
    name: str = "assistant"
    description: str = "Helps a person plan a trip"
    version: str = "0.1.0"
    prompt: str = "Help the person plan the trip. To book a flight, propose it with `propose_booking`: you cannot book, the person approves."
    model: ModelSettings = Gemini()
    tools: list = [search_flights, propose_booking]  # both only read or propose
    input_model: ClassVar[type[AgentInput]] = Search
    output_model: ClassVar[type[AgentOutput]] = Reply
```

[The approvals spec](../specs/approvals.md) says what is left out on purpose, and why.
