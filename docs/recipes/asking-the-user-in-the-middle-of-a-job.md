# Asking the user in the middle of a job

A job runs without a person, so it cannot stop and wait for an answer. It asks at a boundary instead: the first stage ends with its questions, the job succeeds, and when the person answers a new job picks up from the finished stage, which costs nothing again. Nothing is held while you wait, for a minute or for a week.

```python
import asyncio
from typing import ClassVar

from pydantic import BaseModel

from featherweight import (
    AgentBase,
    AgentInput,
    AgentOutput,
    AgentRunner,
    Gemini,
    JobContext,
    ModelSettings,
    SqliteDatabase,
    Status,
    default_db_path,
    job,
    run_job,
)


class Plan(BaseModel):
    destination: str = ""
    nights: int = 0


class Request(AgentInput):
    request: str


class Draft(AgentOutput):  # also carries `open_questions: list[str]`
    plan: Plan = Plan()


class Planner(AgentBase):
    name: str = "planner"
    description: str = "Plans a trip from a request"
    version: str = "0.1.0"
    prompt: str = "Plan the trip. What you do not know, leave out of the plan and ask in open_questions."
    model: ModelSettings = Gemini()
    input_model: ClassVar[type[AgentInput]] = Request
    output_model: ClassVar[type[AgentOutput]] = Draft


class FinishInput(AgentInput):
    plan: Plan
    answers: dict[str, str]  # the questions, and what the person said


class Itinerary(AgentOutput):
    itinerary: str = ""


class Finisher(AgentBase):
    name: str = "finisher"
    description: str = "Writes the itinerary from a plan and the answers"
    version: str = "0.1.0"
    prompt: str = "Write the itinerary from the plan and the answers."
    model: ModelSettings = Gemini()
    input_model: ClassVar[type[AgentInput]] = FinishInput
    output_model: ClassVar[type[AgentOutput]] = Itinerary


class TripIn(BaseModel):
    request: str
    answers: dict[str, str] = {}


class TripOut(BaseModel):  # the outcome: done, or the questions to ask
    done: bool
    questions: list[str] = []
    itinerary: str = ""


@job
async def plan_trip(ctx: JobContext, input: TripIn) -> TripOut:
    first = await ctx.run(Planner, Request(request=input.request))  # the same input each time: a resume reuses it
    if first.status != Status.SUCCESS:
        raise RuntimeError(first.error)
    draft = first.output
    if draft.open_questions and not input.answers:
        return TripOut(done=False, questions=draft.open_questions)  # the job succeeds: asking is a result
    second = await ctx.run(Finisher, FinishInput(plan=draft.plan, answers=input.answers))
    if second.status != Status.SUCCESS:
        raise RuntimeError(second.error)
    return TripOut(done=True, itinerary=second.output.itinerary)


async def main() -> None:
    request = "A few days somewhere warm in March."
    async with SqliteDatabase(default_db_path()) as db, AgentRunner(db) as runner:
        first = await run_job(
            runner, plan_trip, TripIn(request=request), resumable=True
        )  # resumable: it keeps its steps
        print("first job:", first.status.value, "| done:", first.result.done, "| asks:", first.result.questions)

        # Your application keeps "waiting for an answer", tells the person (a page, a notification), and a day may pass.
        answers = {question: "4 nights" for question in first.result.questions}

        second = await run_job(runner, plan_trip, TripIn(request=request, answers=answers), resume_from=first.job_id)
        print("second job:", second.status.value, "| done:", second.result.done, "|", second.result.itinerary)


if __name__ == "__main__":
    asyncio.run(main())
```

- **Asking is a result, not a pause.** `open_questions` is part of every agent's output, and the job succeeds with a typed outcome. The application sees `done=False`, keeps its own "waiting" row, and asks. There is no status and no exception for it.
- **The second job does not pay for the first stage.** The planner ran with the same input, so the second job answers it from the kept results, with no model call and no tool call. Only the finisher runs, with a small input: the plan and the answers.
- **A rerun costs the work it repeats,** the output tokens and the tools, and not the input it reads again. A provider's cache lasts minutes to about an hour, so a person's answer finds it cold anyway.
- **In a chat,** the streaming agent does the asking in its own turn: a background job that needs something returns `open_questions`, and the streaming agent relays them. Only the streaming agent talks to the user.

[The approvals spec](../specs/approvals.md) has the rule, and what is not supported, with the conditions to think again.
