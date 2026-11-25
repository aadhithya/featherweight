# Approvals: agents propose, code executes

Status: implemented as a pattern, in [issue #271](https://github.com/aadhithya/featherweight/issues/271). There is no new class or argument: it is how the parts that exist are put together. How to use it is in [the agents guide](../agents.md#approvals-agents-propose-your-code-acts), [a recipe](../recipes/approving-what-an-agent-proposes.md) and [a chapter of the book](../book/05-in-production/06-approvals.md).

- [Goal](#goal)
- [The rule](#the-rule)
- [The proposal](#the-proposal)
- [The executor](#the-executor)
- [Who talks to the user](#who-talks-to-the-user)
- [Asking in a job: ask at a boundary](#asking-in-a-job-ask-at-a-boundary)
- [Two ways to propose](#two-ways-to-propose)
- [Decisions](#decisions)
- [Not supported](#not-supported)
- [Tests](#tests)

## Goal

Some things must not happen without a person's yes: a booking, a payment, an email, a deletion. featherweight's answer is **two stages**. An agent does its research and comes back with a **proposal**; the application shows it to a person; and after the yes, something that is not the agent executes it: plain code (an API call, a pipeline), another job, or another agent. The agent never holds the permission, so a mistake, or a prompt injection, can only produce a proposal.

This is the principle of the framework applied to approvals ([seams, not solutions](../book/00-start-here/03-philosophy.md)): the proposal is a typed value that Pydantic checks, and who may approve, what is shown and how it is executed are the application's. The reference is the `fw-concierge` application, where a booking is created only by `POST /api/bookings`: "plain application code: no agent decides", with an idempotency key and a row lock.

## The rule

1. **An agent does not ask for approval and does not hold a permission.** It proposes.
2. **Do not give an agent a tool that does what needs approval.** Its [allow-list](agents.md) holds read tools and, if it proposes in the middle of a conversation, a `propose_*` tool that only writes a proposal. A sub-agent or a background job has its own tools and never its parent's, so "read-only" is a matter of what is in its list.
3. **The application, not the model, decides what is shown, who may approve and how the action runs.**

## The proposal

- **A typed value:** a Pydantic model. It is the `output_model` of the agent, or what a `propose_*` tool writes. It is validated when the agent answers, so a malformed proposal never reaches a person.
- **Kept by the application,** with its hash, an expiry and a status (`pending`, `approved`, `rejected`, `expired`). The run log keeps only hashes, and never the content ([persistence](persistence.md)); the result store is the application's.
- **Shown from the typed value.** The approval screen is drawn by your code from the fields of the proposal. A text that a model wrote about it is decoration: the concierge writes its confirmation text from the verified provider and slot, and the person confirms the slot.
- **Referenced by id.** The client sends back the id, the choice (an option of the proposal) and the hash it was shown, never the arguments of the action.

## The executor

A function of your code, with no model in it. For one approval it:

1. **finds the proposal by id** and refuses one that is not `pending` or has expired;
2. **checks the hash** that the person was shown against the stored proposal (the proposal did not change), and that the choice is one of its options;
3. **checks that the approver may approve this:** the approver's identity, from your authentication, never anything the model said ([identity](identity.md), [who is asking](../agents.md#who-is-asking-the-identity-of-a-run));
4. **revalidates what can have changed** (the slot is still free, the price is still the price);
5. **executes once:** an idempotency key makes a second approval of the same proposal return the first result and change nothing, and a lock or a transaction makes two approvals of one thing safe;
6. **records who approved what and when** (your records; the run log holds hashes only).

**It never executes arguments that came from the model's prose or from the client's conversation:** a chat sends its whole history back, so anything in it can have been changed.

## Who talks to the user

**Only the streaming agent talks to the user.** A sub-agent or a background job never asks: it returns a result, `open_questions` or a proposal to its parent, and the streaming agent relays it in its own turn (a background job's result arrives as a message, [background jobs](background-jobs.md)). A pipeline reaches the user through the application: a notification, a page, then a new job. This is the shape of the major products: their background work only reads, and comes back to the user for permission to write.

## Asking in a job: ask at a boundary

An agent that needs something from the user does not stop in the middle of its loop. It **ends its run** with the typed work it has done and its `open_questions` (a successful run; `AgentOutput` carries the field). The job returns a typed outcome (what is done, the questions, the draft) and **succeeds**. The application keeps its own "awaiting" row and tells the user; when they answer, it submits a new job with `resume_from` the old one ([resuming](jobs.md#resuming-a-job)): the finished stage is answered from the result store, with **no model call and no tool call**, and the next stage runs with the compact findings and the answers.

- **Nothing is held while waiting:** no run, slot, lease or timeout. A day is as good as a minute.
- **What a rerun wastes** is the output tokens that it regenerates and the tools that it calls again, not the input that it reads again: `resume_from` avoids both. A provider's cache lasts minutes to about an hour (check the provider), so a person's answer finds a cold cache whatever the framework does.
- **A stage that carries a summary** costs less than a replay of the whole transcript.

## Two ways to propose

- **The output is the proposal.** For a job or a pipeline: the agent's `output_model` is the proposal (`options`, a recommendation, `open_questions`). The run ends, and the application takes it from there.
- **A `propose_*` tool.** For a chat: the agent calls a tool that writes a proposal to your store and answers "proposed as #id, waiting for the user". The tool cannot act, so it is safe in the allow-list. The agent may propose several things, and the interface shows a card for each. After the approval your code executes, and the outcome is added to the conversation as a message for the next turn.

## Decisions

- **Agents propose, code executes.** The only design that needs no suspended run, so a proposal can wait as long as it must and no worker is held. It also makes the model unable to act: the permission is not in its hands.
- **The proposal is data, and is bound by its hash.** An approval means "this exact thing", not "something like it".
- **No approval flag on a tool, and no policy engine.** What needs approval is the application's rule, and it is written where the proposal is made and where it is executed.
- **No new status for a job.** A status says how an execution ended; "needs input" is the meaning of a result, so it is in the typed outcome of a job that succeeded.
- **Read-only background work.** The tools of a child are its own, so a background job reads and proposes.

## Not supported

Each is left out on purpose, and what to do instead is in the right column. The last column says when to think again.

| Not supported | Do this instead | Think again when |
|---|---|---|
| A flag or policy on a tool ("needs approval", read-only) | A tool that does not act, and an executor that does | Many applications write the same policy by hand, and a seam that is not a solution is clear |
| A status or an exception "needs input" for a job | A typed outcome of a successful job, and `resume_from` | Never for the status; an exception that pauses is suspension |
| **Pausing a live run in the middle of its loop** (LangGraph `interrupt` with a checkpointer, OpenAI `RunState`, Pydantic AI deferred tools) | Stages, and a new job with `resume_from`; for a chat, the turn ends and the next turn starts | A chat needs to continue the same turn: see below |
| **Replaying a run's messages** (an `AgentResult.messages`, an `AgentRunner.from_messages`, an input that builds its first messages) | Typed state in the output; tools backed by your own cache, so that a rerun repeats model calls and not tool calls | A real long loop that needs its verbatim tool outputs, and stages do not express it. Then: opt-in for one agent, stored by the application, on the input and not on the runner, with a check of the agent's hash |

**The honest limit:** one long agent loop that asks at its fortieth step and needs its verbatim tool outputs after the answer. A replay would cascade (a pipeline of seven agents has seven histories), is valid only for the same agent (its prompt, tools, tool-call ids and reasoning signatures), puts content in memory, and a runner method would grow the runner. Stages cover almost every case.

**For a chat,** AG-UI 1.0 has a model for a pause that is a turn ending: a run can finish with an interrupt outcome (an id, a reason, a message, a `tool_call_id`, a `response_schema`, an expiry), and the next request carries `resume` entries. featherweight does not use it yet: it emits the success and cancelled outcomes, ignores `resume`, and a `ChatInput` must end with a user message. A later feature for streaming agents, spec first, with the rule that an interrupt names a proposal stored on the server and `resume` carries only the decision.

## Tests

- The recipe [approving what an agent proposes](../recipes/approving-what-an-agent-proposes.md), with a fake model: the agent has no tool that books; a second approval books once; a changed proposal, an expired one and an unauthorised approver are refused; the `propose_*` variant writes a proposal and acts on nothing.
- The recipe [asking the user in the middle of a job](../recipes/asking-the-user-in-the-middle-of-a-job.md): a job ends with questions and succeeds; the second job reuses the first stage, so its model is called once across both.
- The book's chapter 5-6, whose code needs no model: the proposal and the executor run, and are checked for a double approval and a changed proposal.
