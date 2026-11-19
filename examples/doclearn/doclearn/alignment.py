"""The Semantic Alignment agent: one unit (the same section of every document) in, its learned subtree out."""

from typing import ClassVar

from pydantic import Field, model_validator

from doclearn.facts import find_spans, walk
from doclearn.models import ElementType, FieldInfo, LearnedBlock, LearnedNode, UnitDocument
from featherweight import AgentBase, AgentInput, AgentOutput, Gemini, ModelSettings

MIN_COVERAGE_DIVISOR = 2  # the learned tree must have at least 1/2 of the largest document's blocks

PROMPT = """\
You align one section of several documents of the same category into one learned section. The input is JSON with \
the category, the `position` of the section, `documents` (for each document that has this section: its \
`node` tree and the `fields` values used in it), `missing_in` (the documents that do not have this section) and \
`styles` (the formatting pattern of each element type used).

Return:
1. `blocks`: the learned section as a flat list of blocks.
2. `fields`: a description of every field the blocks use.

Rules for `blocks`:
- The list is flat but describes a tree. Every block has an `id` (b1, b2, b3, ...) and a `parent_id`: the id of the \
block it is nested under, or "" for the single root block, which is the section heading itself. List the blocks in \
document order, each parent before its children.
- The blocks must cover the whole section: every block of every document is matched into exactly one learned block, \
nested like the input. Never return only the root block: the content is in the blocks below it.
- Match blocks across the documents that make the same point, even when worded differently, and return one learned \
block for each. Keep the order of the documents and the element `type` of the input. Use only the element types that \
appear in the input, and never add a block that is in none of the documents. Do not change the structure or the style.
- `text` is the canonical wording of the block: the clearest wording found in the documents, copied exactly, in the \
style of the source. Case-specific values stay {{span_name}}, using the span names from the input.
- `generated`: true if the content of the block varies with the case itself (the facts of the case, a narrative, a justification) and \
has to be written for each case; then `text` is one representative example. False for standard wording, also when it \
contains {{spans}} for case-specific values. The `text` of every block is copied from one of the documents, never \
written by you.
- `alternatives`: the other wordings of the same block found in other documents, as they are written. Empty if \
every document words it the same way.
- `conditions`: a list, empty for most blocks. Set it only on a block that some documents have and others do not \
(see `missing_in`, or blocks present in only some documents). If the field values you can see explain why (for \
example salary >= 5000, or jurisdiction = Bayern), use that. Otherwise introduce a boolean field such as \
include_works_council_section and use the condition `= true`. Never put a condition on a block that every document \
has. All conditions of a block must hold, and they cover the block and everything under it.

Rules for `fields`:
- Describe every {{span}} used in any `text` or `alternatives`, and every field used in a condition: its `name`, its \
`type` (text, number, date or boolean), a short one-line `description` that tells a user what to enter, and \
`calculated` = true if the value is computed from other fields, such as totals and subtotals.
- A field is a fact about the case that a user knows. Never describe a field about formatting, wording or element \
types, and never use a condition for such a difference.
"""


class SemanticAlignmentInput(AgentInput):
    category: str = Field(description="The document category, provided by the user.")
    position: int = Field(
        description="Which section this is, starting at 1: the same section is matched across the documents by its heading."
    )
    documents: list[UnitDocument] = Field(description="The documents that have this unit, each with its subtree.")
    missing_in: list[str] = Field(description="Ids of the documents that do not have this unit.")
    styles: list[ElementType] = Field(description="Reconciled formatting of each element type used in the subtree.")


class SemanticAlignmentOutput(AgentOutput):
    blocks: list[LearnedBlock] = Field(
        default_factory=list, description="The learned section as a flat list of blocks."
    )
    fields: list[FieldInfo] = Field(default_factory=list, description="Every field the blocks use, described.")

    @model_validator(mode="after")
    def _consistent(self) -> "SemanticAlignmentOutput":
        """Rules the schema cannot express, checked on the output alone. A failure raises and the runner retries."""
        problems = []

        names = [f.name for f in self.fields]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            problems.append(f"fields described twice: {duplicates}")
        described = set(names)

        ids = [b.id for b in self.blocks]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            problems.append(f"block ids used twice: {repeated}")
        if self.blocks:
            roots = [b.id for b in self.blocks if b.parent_id == ""]
            if len(roots) != 1:
                problems.append(f"there must be exactly one root block (parent_id empty), found {len(roots)}")
            unknown = sorted({b.parent_id for b in self.blocks if b.parent_id and b.parent_id not in set(ids)})
            if unknown:
                problems.append(f"parent_id refers to unknown blocks: {unknown}")
            if not repeated and len(roots) == 1 and not unknown:
                reachable = _reachable(self.blocks, roots[0])
                lost = sorted(set(ids) - reachable)
                if lost:
                    problems.append(f"blocks not connected to the root: {lost}")

        undescribed: set[str] = set()
        for block in self.blocks:
            own = set(find_spans(block.text))
            alternative_spans = {s for alt in block.alternatives for s in find_spans(alt)}
            undescribed |= (own | alternative_spans) - described
            undescribed |= {c.field for c in block.conditions} - described
        if undescribed:
            problems.append(f"fields used but not described: {sorted(undescribed)}")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def to_node(self) -> LearnedNode:
        """Rebuild the tree from the flat blocks. Children keep the order of the list."""
        nodes = {
            b.id: LearnedNode(
                type=b.type, text=b.text, kind=_kind(b), alternatives=b.alternatives, conditions=b.conditions
            )
            for b in self.blocks
        }
        root = None
        for block in self.blocks:
            if block.parent_id == "":
                root = nodes[block.id]
            else:
                nodes[block.parent_id].children.append(nodes[block.id])
        if root is None:
            raise ValueError("no blocks returned")
        return root


def _kind(block: LearnedBlock) -> str:
    """The kind of a block, from what the agent decided and what the text says: the agent only judges whether the
    content varies with the case (`generated`); whether the block has {{spans}} is read off its wording.

    Asked for the kind itself, the agent contradicted its own text in 13 of 22 failed attempts (`slot` with no span,
    `fixed` with a span), for example for a block whose wordings differ in whether they have a span at all.
    """
    if block.generated:
        return "generated"
    has_span = bool(find_spans(block.text)) or any(find_spans(a) for a in block.alternatives)
    return "slot" if has_span else "fixed"


def _reachable(blocks: list[LearnedBlock], root_id: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for block in blocks:
        children.setdefault(block.parent_id, []).append(block.id)
    seen, stack = set(), [root_id]
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(children.get(current, []))
    return seen


class SemanticAlignmentAgent(AgentBase):
    name: str = "semantic_alignment"
    description: str = (
        "Aligns one section across documents into a learned subtree with kinds, alternatives and conditions"
    )
    version: str = "0.6.1"
    prompt: str = PROMPT
    model: ModelSettings = Gemini(model="gemini-3.8-flash", thinking="low")
    input_model: ClassVar[type[AgentInput]] = SemanticAlignmentInput
    output_model: ClassVar[type[AgentOutput]] = SemanticAlignmentOutput

    def check_output(self, agent_input: AgentInput, output: AgentOutput) -> None:
        """Reject output that drops content: the tree must not be far smaller than the largest document's section.

        Matching merges blocks across documents, so the learned tree can be smaller than the largest
        document (a repeated list item may be learned once), but not by more than half.
        """
        if not output.blocks:
            raise ValueError("no blocks returned")
        largest = max((len(list(walk([d.node]))) for d in agent_input.documents), default=0)
        learned = len(output.blocks)
        if learned * MIN_COVERAGE_DIVISOR < largest:
            returned = [f"{b.id}<-{b.parent_id or 'root'}:{b.text[:25]}" for b in output.blocks[:5]]
            raise ValueError(
                f"the learned tree has {learned} blocks but the largest document has {largest}: it must cover "
                f"every block of every document, nested like the input. Returned: {returned}"
            )
