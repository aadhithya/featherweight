"""The generation pipeline: a learned document tree and the facts of a new case in, markdown out.

Code first compiles the tree (`compile_tree`): the blocks whose conditions do not hold for the facts are cut out, so
the agent never sees them. One Generator agent then writes the whole document: it copies `fixed` blocks, copies `slot`
blocks with the facts filled in (small grammatical adaptations such as gender are allowed, a fact is never changed),
writes `generated` blocks in the style of the example, and follows the tree for structure and order. It has the
calculate and date tools and `check_document`, the only way to hand in the document: the tool checks it in code
(`check_generated`, no model) and keeps the accepted document in the run store. A document that passes is accepted;
after `MAX_CHECKS` failed checks the last one is accepted with its problems, which are reported. The agent returns only
its notes: a run that ends without an accepted document fails. Code checks the required inputs before any model call.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping
from difflib import SequenceMatcher
from typing import ClassVar

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from doclearn.assembly import LEADING_NUMBER, conditions_hold
from doclearn.facts import SPAN_RE, find_spans, walk
from doclearn.models import ElementType, FieldValue, InputField, LearnedNode, LearnedTree
from doclearn.store import LearnedDocument
from micro.context import RunContext
from micro.model_settings import Gemini, ModelSettings
from micro.models import AgentBase, AgentInput, AgentOutput, RunStatus
from micro.runner import AgentRunner
from micro.tools import NO_JOB, add_to_date, calculate, current_datetime, days_between

logger = logging.getLogger(__name__)

MAX_CHECKS = 3  # failed checks of `check_document` before the agent is told to stop and report the problems
MIN_SIMILARITY = 0.85  # how close a fixed or slot line must stay to its wording (small adaptations, not rewrites)
STATE_KEY = "generation"  # in the run store: the compiled tree, the facts and the number of failed checks
MAX_VERBATIM_CHARS = 80  # longer values are descriptions, which generated blocks paraphrase

PROMPT = """\
You write one document from a learned structure and the facts of a new case. The input is JSON with \
`category`, `structure` (the document as an outline), `styles` (the markdown pattern of each element type), \
`fields` (the facts a user can give: name, type, description, whether required, whether calculated), `facts` (the \
values provided) and optional `guidance` from the user.

Follow `structure`: the same blocks in the same order, with the same nesting and markdown. Each line of the outline \
is a block: its kind, its element type (`styles` has the pattern of each type), its text and other wordings (`| or`).
- `fixed`: copy the text. You may make small grammatical adaptations that the facts require (gender, number, an \
article), nothing else.
- `slot`: the same, and replace each {{name}} with the value of that fact. If a fact was not provided, use another \
wording of the block that does not need it; of the possible wordings use the one that uses the most provided facts.
- `generated`: the block varies with the case. Write it in the style of the example text, from the facts and the \
guidance only. The example shows the style and form, not the content: never reuse its subject matter, names, \
products, places or amounts.
- A calculated field (a total or subtotal) is computed from the other facts with the `calculate` tool. For dates \
given in relative terms use the date tools.

Where the wordings of a block differ in gender, number or the like, choose the one that fits the facts and the \
guidance (the pronoun for the person named, for example). Never change a fact: names, dates, amounts and numbers \
are used exactly as given. Never invent names, dates, \
amounts, allegations or other facts: a generated block states only what the facts and the guidance support, even \
where the example text says more. If a generated block cannot be written from the facts and the guidance, leave it \
out. Put in `notes` what you left out, and which wording you chose where a fact was missing.

Hand in the document by calling `check_document` with the whole document; there is no other way to deliver it. If \
it reports problems, fix them and call it again. After OK or FAILED the document is delivered: do not call it again. \
After FAILED list the remaining problems in `notes`. Then return `notes`.
"""


class GenerationError(Exception):
    """The document cannot be generated: bad inputs or an agent failure."""


@tool
async def check_document(markdown: str, runtime: ToolRuntime[RunContext]) -> str:
    """Hand in the whole document you wrote. It is checked against the structure and the facts: if it passes it is
    delivered, if not you get the problems back; fix them and hand it in again."""
    ctx = runtime.context
    if ctx.job_id is None:
        return NO_JOB
    state = await ctx.store.get(ctx.job_id, STATE_KEY)
    if state is None:
        return "There is nothing to check the document against."
    problems = check_generated(LearnedTree.model_validate(state["tree"]), state["facts"], markdown)
    if not problems:
        state.update(document=markdown, problems=[])
        await ctx.store.set(ctx.job_id, STATE_KEY, state)
        return "OK: the document is delivered. Return your notes."
    state["failed_checks"] += 1
    listed = "\n".join(f"- {p}" for p in problems)
    if state["failed_checks"] >= MAX_CHECKS:
        state.update(document=markdown, problems=problems)
        await ctx.store.set(ctx.job_id, STATE_KEY, state)
        return f"FAILED after {MAX_CHECKS} checks: the document is delivered as it is. List these problems in `notes`:\n{listed}"
    await ctx.store.set(ctx.job_id, STATE_KEY, state)
    return f"Problems ({state['failed_checks']} of {MAX_CHECKS} failed checks):\n{listed}"


class GeneratorInput(AgentInput):
    category: str = Field(description="The document category.")
    structure: str = Field(description="The learned document tree as an outline: one block per line, nested by indent.")
    styles: list[ElementType] = Field(description="The markdown pattern of each element type.")
    fields: list[InputField] = Field(description="The facts a user can give, and the calculated ones.")
    facts: list[FieldValue] = Field(description="The values provided for this case.")
    guidance: str = Field(default="", description="Optional extra instructions from the user.")


class GeneratorOutput(AgentOutput):
    notes: list[str] = Field(default_factory=list, description="What was left out and which wordings were chosen.")


class GeneratorAgent(AgentBase):
    name: str = "document_generator"
    description: str = "Writes a document from a learned tree and the facts of a new case"
    version: str = "0.4.0"
    prompt: str = PROMPT
    model: ModelSettings = Gemini(model="gemini-3.8-flash", thinking="low")
    tools: list[BaseTool] = [calculate, current_datetime, add_to_date, days_between, check_document]
    input_model: ClassVar[type[AgentInput]] = GeneratorInput
    output_model: ClassVar[type[AgentOutput]] = GeneratorOutput


class GeneratedDocument(BaseModel):
    markdown: str
    notes: list[str] = Field(
        default_factory=list, description="From the agent: what it left out, which wordings it chose."
    )
    not_provided: list[str] = Field(default_factory=list, description="Optional inputs that had no value.")
    unused_inputs: list[str] = Field(
        default_factory=list,
        description="Inputs that were given but whose value is nowhere in the document (the template has no place "
        "for it, or the agent left it out). Flags are not listed.",
    )
    problems: list[str] = Field(
        default_factory=list,
        description="What the check still found in the delivered document after the failed checks (empty if it passed).",
    )


# the structure the agent follows


def compile_tree(tree: LearnedTree, facts: Mapping[str, str]) -> LearnedTree:
    """The tree for these facts: a block whose conditions do not hold (a missing value does not hold) is cut out with
    everything under it, and the conditions of the blocks that stay are dropped. The input is not modified."""
    by_name = {f.name: f for f in tree.fields}

    def keep(nodes: list[LearnedNode]) -> list[LearnedNode]:
        return [
            node.model_copy(update={"conditions": [], "children": keep(node.children)})
            for node in nodes
            if conditions_hold(node, dict(facts), by_name)
        ]

    return tree.model_copy(update={"nodes": keep(tree.nodes)})


def render_structure(tree: LearnedTree) -> str:
    """The tree as an outline, one block per line: kind, element type, text (JSON, so the indentation of the text is
    kept), other wordings and conditions. The indent of the line is the nesting of the block."""
    lines: list[str] = []

    def visit(node: LearnedNode, depth: int) -> None:
        parts = [f"{'  ' * depth}{node.kind} {node.type} {json.dumps(node.text, ensure_ascii=False)}"]
        parts += [f"| or {json.dumps(alternative, ensure_ascii=False)}" for alternative in node.alternatives]
        parts += [f"| only if {c.field} {c.op} {json.dumps(c.value, ensure_ascii=False)}" for c in node.conditions]
        lines.append(" ".join(parts))
        for child in node.children:
            visit(child, depth + 1)

    for root in tree.nodes:
        visit(root, 0)
    return "\n".join(lines)


# the checks that need no model


def _line(text: str) -> str:
    return LEADING_NUMBER.sub("", " ".join(text.split()))  # numbering is positional: items may be renumbered


def _lines(text: str) -> list[str]:
    return [_line(line) for line in text.splitlines() if line.strip()]


def _renderable(node: LearnedNode, facts: Mapping[str, str]) -> list[tuple[list[str], list[str]]]:
    """The wordings of a block whose spans all have a value: the filled-in lines, and the values filled in."""
    out = []
    for wording in [node.text, *node.alternatives]:
        names = find_spans(wording)
        if all(facts.get(name, "").strip() for name in names):
            out.append(
                (_lines(SPAN_RE.sub(lambda m: facts[m.group(1)].strip(), wording)), [facts[n].strip() for n in names])
            )
    return out


def _find(document: list[str], wording: list[str], start: int = 0) -> int:
    """Where lines close to the wording's lines come in a row in the document (at or after `start`); -1 if nowhere."""
    expected = "\n".join(wording).casefold()
    for i in range(start, len(document) - len(wording) + 1):
        if (
            SequenceMatcher(None, expected, "\n".join(document[i : i + len(wording)]).casefold()).ratio()
            >= MIN_SIMILARITY
        ):
            return i
    return -1


def check_generated(tree: LearnedTree, facts: Mapping[str, str], markdown: str) -> list[str]:
    """What is wrong with a generated document, checked in code (no model). `tree` is the compiled tree.

    No `{{placeholder}}` is left. Every `fixed` or `slot` block is there, in the order of the tree, as lines that
    stay close to one of its wordings that the facts can fill (small adaptations such as gender are fine), and every
    fact filled into a block is in its lines exactly as given. A `generated` block cannot be checked: it varies with
    the case. Leading list numbers are ignored.
    """
    problems = []
    left = sorted({m for m in SPAN_RE.findall(markdown)})
    if left:
        problems.append(f"placeholders left unresolved: {left}")

    document = _lines(markdown)
    cursor = 0

    for node in walk(tree.nodes):
        if node.kind == "generated":
            continue
        wordings = _renderable(node, facts)
        if not wordings:
            continue
        found = [(_find(document, lines, cursor), lines, values) for lines, values in wordings]
        found = [f for f in found if f[0] >= 0]
        if found:
            position, lines, values = min(found, key=lambda f: f[0])
            cursor = position + len(lines)
            text = " ".join(document[position : position + len(lines)])
            for value in values:
                if " ".join(value.split()) not in text:
                    problems.append(f"a fact was changed: {value!r} in {node.text.strip()[:60]!r}")
        elif any(_find(document, lines) >= 0 for lines, _ in wordings):
            problems.append(f"out of order: {node.text.strip()[:60]!r}")
        else:
            problems.append(f"missing or too different: {node.text.strip()[:60]!r}")
    return problems


# the pipeline


def unused_inputs(tree: LearnedTree, facts: Mapping[str, str], markdown: str) -> list[str]:
    """The given inputs whose value is not in the document. Booleans (flags) are not meant to appear, and a long
    value (a description a generated block paraphrases) is not expected verbatim; values are compared with whitespace
    normalised. A real run silently dropped an amount and a number of days that the learned tree had no slot for."""
    types = {f.name: f.type for f in tree.fields}
    document = " ".join(markdown.split())

    def appears(value: str) -> bool:  # as a whole word or number: "41" is not in "2041"
        return re.search(rf"(?<!\w){re.escape(' '.join(value.split()))}(?!\w)", document) is not None

    return [
        name
        for name, value in facts.items()
        if types.get(name) != "boolean" and len(value) <= MAX_VERBATIM_CHARS and not appears(value)
    ]


def _clean(facts: Mapping[str, str]) -> dict[str, str]:
    return {name: str(value).strip() for name, value in facts.items() if str(value).strip()}


async def generate_document(
    runner: AgentRunner,
    learned: LearnedDocument,
    facts: Mapping[str, str],
    guidance: str = "",
    *,
    job_id: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> GeneratedDocument:
    """Generate a document from a learned tree and the facts of a new case.

    Raises `GenerationError` before any model call if a fact is not a field of the tree, or a required input is
    missing, and if the Generator fails. The agent checks its own document with `check_document` and reports in
    `notes` what it could not fix. Run it through `micro.jobs.run_job` like the learning pipeline. Optional inputs
    without a value are listed in `not_provided`. `progress`, if given, is called with a line before the agent runs.
    """
    schema = learned.facts_schema
    given = _clean(facts)

    unknown = sorted(set(given) - {f.name for f in learned.tree.fields})
    if unknown:
        raise GenerationError(f"Unknown inputs (not fields of the learned tree): {unknown}")
    missing = [f.name for f in schema.fields if f.required and f.name not in given]
    if missing:
        raise GenerationError(f"Required inputs are missing: {missing}")
    not_provided = [f.name for f in schema.fields if not f.required and f.name not in given]

    tree = compile_tree(learned.tree, given)  # the blocks whose conditions do not hold are cut out
    required = {f.name for f in schema.fields if f.required}
    agent_input = GeneratorInput(
        category=tree.category,
        structure=render_structure(tree),
        styles=learned.template.element_types,
        fields=[InputField(**f.model_dump(), required=f.name in required) for f in learned.tree.fields],
        facts=[FieldValue(name=n, value=v) for n, v in given.items()],
        guidance=guidance.strip(),
    )
    logger.info(
        "generation_started learned_id=%s category=%s facts=%d not_provided=%d job_id=%s",
        learned.learned_id,
        tree.category,
        len(given),
        len(not_provided),
        job_id,
    )

    job_id = job_id or await runner.db.create_job("generate")  # the check tool keeps its state per job
    await runner.store.set(
        job_id,
        STATE_KEY,
        {"tree": tree.model_dump(mode="json"), "facts": given, "failed_checks": 0, "document": None, "problems": []},
    )
    if progress:
        progress("Writing the document")
    result = await runner.run(GeneratorAgent, agent_input, job_id=job_id)
    if result.status != RunStatus.SUCCESS:
        raise GenerationError(f"The Generator failed: {result.error}")
    state = await runner.store.get(job_id, STATE_KEY)
    if state is None or state["document"] is None:
        raise GenerationError(
            "The Generator ended without handing in a document that passed the check (check_document)"
        )
    markdown = state["document"]
    unused = unused_inputs(tree, given, markdown)
    if unused:
        logger.warning("generation_unused_inputs inputs=%s job_id=%s", unused, job_id)
    logger.info(
        "generation_finished learned_id=%s failed_checks=%d problems=%d job_id=%s",
        learned.learned_id,
        state["failed_checks"],
        len(state["problems"]),
        job_id,
    )
    return GeneratedDocument(
        markdown=markdown,
        notes=result.output.notes,
        not_provided=not_provided,
        unused_inputs=unused,
        problems=state["problems"],
    )
