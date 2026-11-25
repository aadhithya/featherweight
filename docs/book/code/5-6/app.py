"""Chapter 5-6: approvals. An agent proposes, and plain code acts after a person says yes. Nothing here needs a model."""

import hashlib
import time

from pydantic import BaseModel

APPROVERS = {"ana"}  # yours: who may approve a rollback, from your own authentication
PROPOSALS: dict[str, dict] = {}  # yours: your database
DONE: dict[str, str] = {}  # idempotency key -> what was done


class Rollback(BaseModel):  # what the agent returns as its output: a proposal, not an action
    service: str
    to_version: str
    reason: str


def keep(proposal: Rollback, seconds: int = 1800) -> dict:
    """Stage one is over: keep the proposal with its hash and an expiry, and show it to a person."""
    shown = hashlib.sha256(proposal.model_dump_json().encode()).hexdigest()
    record = {"id": "p1", "proposal": proposal, "hash": shown, "expires": time.time() + seconds, "status": "pending"}
    PROPOSALS[record["id"]] = record
    return record


def approve(proposal_id: str, shown_hash: str, approver: str, key: str) -> str:
    """Stage two: plain code, with no model in it. It checks, then acts once."""
    if key in DONE:
        return DONE[key]  # the same approval again changes nothing
    record = PROPOSALS[proposal_id]
    if record["status"] != "pending" or time.time() > record["expires"]:
        raise PermissionError("it is not pending, or it has expired")
    if shown_hash != record["hash"]:
        raise PermissionError("it is not the proposal that was shown")
    if approver not in APPROVERS:
        raise PermissionError(f"{approver} may not approve")
    record["status"] = "approved"
    proposal = record["proposal"]
    DONE[key] = f"rolled {proposal.service} back to {proposal.to_version}"  # an API call, in real life
    return DONE[key]


def main() -> None:
    record = keep(Rollback(service="payment-form", to_version="4.1", reason="errors began with 4.2"))
    print("proposed:", record["proposal"].model_dump_json())
    for who, shown, key in [
        ("raj", record["hash"], "k0"),
        ("ana", "0" * 64, "k1"),
        ("ana", record["hash"], "k2"),
        ("ana", record["hash"], "k2"),
    ]:
        try:
            print(who, "->", approve(record["id"], shown, who, key))
        except PermissionError as exc:
            print(who, "-> refused:", exc)


if __name__ == "__main__":
    main()
