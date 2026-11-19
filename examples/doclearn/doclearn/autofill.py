"""Fill the form with an agent: invent one case for a learned template, with a story the Generator can use as guidance.

The Form Filler gets the inputs of the category (the facts schema) and the learned structure, and returns a value for
every input the case needs plus a story. Code checks the result (`check_form`: known names, every required input,
values that fit their type, a story); problems go back to the agent for at most `MAX_RETRIES` more attempts, like the
Generator.
"""

import logging
from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel, Field

from doclearn.assembly import _value_fits
from doclearn.generation import render_structure
from doclearn.models import FieldValue, InputField
from doclearn.store import LearnedDocument
from featherweight import AgentBase, AgentInput, AgentOutput, AgentRunner, Gemini, ModelSettings, RunStatus

logger = logging.getLogger(__name__)

MAX_RETRIES = 2  # after the first attempt, with the problems of the previous one

PROMPT = """\
You invent a realistic, fictional case so that a document can be written for it. The input is JSON with \
`category`, `structure` (the document the case is for, as an outline), `fields` (the inputs of the document: name, \
type, description, whether required) and, if the previous attempt had problems, `problems` and `previous`.

Return in `facts` a value for each field and in `story` the story of the case.
- Invent one case that hangs together: the people, companies, places, dates, amounts and events agree with each \
other, and the dates are in a plausible order. Use made-up names; never a real person or company.
- Give a value for every required field. Give a value for an optional field when the story supports it and leave it \
out when it does not. A boolean field is a flag the structure tests with `| only if`: give `true` or `false` so that \
the document follows the story.
- Write each value the way the document would: a `text` as plain text; a `number` as an amount with its currency or \
unit where the field description implies one; a `date` as full month name, day and year (for example `March 4, 2024`); \
a `boolean` as `true` or `false`. A value is a short phrase or a few lines, never a paragraph.
- The `story` is one to three paragraphs of plain text for the person who writes the document: what happened and in \
what order, what each party says, what is requested or proposed, and the tone to keep. It adds the narrative that the fields cannot \
hold and states nothing that contradicts a value. The `structure` shows the kind of document and the sections it \
has: the story gives the material for the parts that are written for each case, and nothing the structure does not \
call for.
- Use each field name exactly as given. If `problems` is given, fix them in a new answer.
"""


class FormFillerInput(AgentInput):
    category: str = Field(description="The document category.")
    structure: str = Field(description="The learned document tree as an outline: one block per line, nested by indent.")
    fields: list[InputField] = Field(description="The inputs of the document.")
    problems: list[str] = Field(default_factory=list, description="What was wrong with the previous attempt.")
    previous: str = Field(default="", description="The previous attempt, when there were problems.")


class FormFillerOutput(AgentOutput):
    facts: list[FieldValue] = Field(description="The values of the invented case, by field name.")
    story: str = Field(description="The story of the case, to guide the writing of the document.")


class FormFillerAgent(AgentBase):
    name: str = "form_filler"
    description: str = "Invents a case for a learned template: the values of its inputs and a story"
    version: str = "0.1.0"
    prompt: str = PROMPT
    model: ModelSettings = Gemini(model="gemini-3.5-flash-lite", thinking="low")
    input_model: ClassVar[type[AgentInput]] = FormFillerInput
    output_model: ClassVar[type[AgentOutput]] = FormFillerOutput


class AutofillError(Exception):
    """The form cannot be filled: an agent failure, or problems left after the retries."""


class FilledForm(BaseModel):
    facts: dict[str, str] = Field(description="Input name to value; no empty values.")
    story: str


def check_form(fields: list[InputField], facts: list[FieldValue]) -> list[str]:
    """What is wrong with the values of a form, checked in code (no model): known names, each given once, every
    required input has a value, each value fits its type. Empty values count as not given."""
    problems = []
    by_name = {f.name: f for f in fields}
    names = [f.name for f in facts]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        problems.append(f"values given more than once: {duplicated}")
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        problems.append(f"not fields of the document: {unknown}")
    given = {f.name: f.value.strip() for f in facts if f.value.strip()}
    missing = [f.name for f in fields if f.required and f.name not in given]
    if missing:
        problems.append(f"required fields without a value: {missing}")
    for name, value in given.items():
        info = by_name.get(name)
        if info and not _value_fits(info.type, value):
            problems.append(f"{name}: '{value}' is not a valid {info.type}")
    return problems


async def fill_form(
    runner: AgentRunner,
    learned: LearnedDocument,
    *,
    job_id: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> FilledForm:
    """Invent a case for a learned template. Raises `AutofillError` if the agent fails or the problems stay after the
    retries. Run it through `featherweight.jobs.run_job` like the other pipelines."""
    schema = learned.facts_schema
    base = FormFillerInput(category=schema.category, structure=render_structure(learned.tree), fields=schema.fields)
    attempt_input = base
    problems: list[str] = []
    for attempt in range(1, MAX_RETRIES + 2):
        if progress:
            progress(
                "Inventing a case"
                if attempt == 1
                else f"Inventing the case again (attempt {attempt}): fixing {len(problems)} problems"
            )
        result = await runner.run(FormFillerAgent, attempt_input, job_id=job_id)
        if result.status != RunStatus.SUCCESS:
            raise AutofillError(f"The Form Filler failed: {result.error}")
        output = result.output
        problems = check_form(schema.fields, output.facts)
        if not output.story.strip():
            problems.append("the story is empty")
        logger.info("autofill_attempt attempt=%d problems=%d job_id=%s", attempt, len(problems), job_id)
        if not problems:
            facts = {f.name: f.value.strip() for f in output.facts if f.value.strip()}
            return FilledForm(facts=facts, story=output.story.strip())
        previous = "\n".join(f"{f.name}: {f.value}" for f in output.facts) + f"\n\n{output.story}"
        attempt_input = base.model_copy(update={"problems": problems, "previous": previous})
    raise AutofillError(f"The form still has problems after {MAX_RETRIES + 1} attempts: {problems}")
