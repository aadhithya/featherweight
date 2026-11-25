<!-- Copied from docs/book/05-in-production/06-approvals.md by scripts/build_skill.py. Edit the original, not this file. -->

# Approvals

An agent proposes, and your code acts after a person says yes.

An agent never books, pays or sends. It returns a proposal, a person approves it, and plain code executes it.

<!-- code: code/5-6/app.py -->
```python
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
```

```
make book-chapter N=5-6
```

The proposal is shown, the wrong approver and a changed proposal are refused, and the right approval acts once, even when it is sent twice:

```
proposed: {"service":"payment-form","to_version":"4.1","reason":"errors began with 4.2"}
raj -> refused: raj may not approve
ana -> refused: it is not the proposal that was shown
ana -> rolled payment-form back to 4.1
ana -> rolled payment-form back to 4.1
```

**What to notice**

- **The agent has no tool that acts.** `Rollback` is its `output_model`, and its allow-list holds read tools. A wrong answer, or a prompt injection, can only produce a wrong proposal. If it proposes in the middle of a conversation, give it a tool that only writes a proposal.
- **The proposal is data.** It is typed, validated when the agent answers, kept by you with its hash and an expiry, and drawn on the screen from its fields. A sentence that the model wrote about it is decoration.
- **An approval is for exactly what was shown.** The client sends the id, the choice and the hash it saw, never the arguments of the action. `approve` refuses a proposal that changed, expired or is not pending, and an approver who may not.
- **The executor has no model in it.** It is plain code: an idempotency key makes a second approval change nothing. Revalidate what can have changed before you act (the slot is still free).
- **Only the streaming agent talks to the user.** A sub-agent or a background job has its own tools, so it reads, and what it finds comes back to the streaming agent as a result, `open_questions` or a proposal.
- **A job asks at a boundary.** Its agent ends with `open_questions`, the job succeeds with a typed outcome, you ask the person, and a new job with `resume_from` reuses the finished stage at no cost. Nothing is held while you wait.

> **Why not pause the run.** A paused run needs its conversation kept somewhere, and a person may take a day. A proposal that is data can wait for as long as it must and holds nothing. What is left out, and when to think again, is in [the approvals spec](https://github.com/aadhithya/featherweight/blob/main/docs/specs/approvals.md).

**Also possible**

- [Approving what an agent proposes](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/approving-what-an-agent-proposes.md): a flight search agent, a typed proposal, an executor, and a tool that proposes.
- [Asking the user in the middle of a job](https://github.com/aadhithya/featherweight/blob/main/docs/recipes/asking-the-user-in-the-middle-of-a-job.md): two stages and `resume_from`.

Next: Part 6 shows how to plug in your own storage.
