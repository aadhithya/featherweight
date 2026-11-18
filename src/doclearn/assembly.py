"""Assembly of the learning pipeline, all in code: stitch the units into the learned tree, check it, derive the
facts schema, and check that the source documents conform to the tree."""

import datetime as dt
import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel, Field

from doclearn.facts import SPAN_RE, find_spans, walk
from doclearn.learning import AlignedUnit, LearningError, ReconciledAnalysis
from doclearn.models import (
    Condition,
    DocumentFacts,
    FactNode,
    FactsSchema,
    FieldInfo,
    InputField,
    LearnedNode,
    LearnedTree,
)

logger = logging.getLogger(__name__)

LEGAL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
ORDERING_OPS = {">", ">=", "<", "<="}
MIN_CONFORMANCE = 0.8  # a document must have at least this share of its blocks matched by the tree
LEADING_NUMBER = re.compile(r"^\d+[.)]\s+")  # numbering is positional, so it is ignored when comparing text


class DocumentConformance(BaseModel):
    document_id: str
    matched: int = Field(description="Blocks of the document that the tree matches.")
    total: int = Field(description="All blocks of the document.")
    mismatches: list[str] = Field(default_factory=list)

    @property
    def score(self) -> float:
        return self.matched / self.total if self.total else 1.0


class Assembly(BaseModel):
    tree: LearnedTree
    facts_schema: FactsSchema
    warnings: list[str] = Field(default_factory=list)  # the soft checks that did not pass
    conformance: list[DocumentConformance] = Field(default_factory=list)


# values


def parse_number(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.\-]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(text: str) -> dt.date | None:
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _value_fits(field_type: str, value: str) -> bool:
    if field_type == "number":
        return parse_number(value) is not None
    if field_type == "date":
        return parse_date(value) is not None
    if field_type == "boolean":
        return value.strip().lower() in {"true", "false"}
    return True


def _holds(condition: Condition, doc_fields: dict[str, str], info: FieldInfo | None) -> bool | None:
    """Does the condition hold for this document? None if the document has no value for the field (a flag)."""
    if info is None or condition.field not in doc_fields:
        return None
    raw = doc_fields[condition.field]
    if info.type == "number":
        left, right = parse_number(raw), parse_number(condition.value)
    elif info.type == "date":
        left, right = parse_date(raw), parse_date(condition.value)
    elif info.type == "boolean":
        left, right = raw.strip().lower(), condition.value.strip().lower()
    else:
        left, right = raw.strip().casefold(), condition.value.strip().casefold()
    if left is None or right is None:
        return None
    if condition.op == "=":
        return left == right
    if condition.op == "!=":
        return left != right
    if info.type not in ("number", "date"):
        return None
    return {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[condition.op]


def _conditions_state(node: LearnedNode, doc_fields: dict[str, str], fields: dict[str, FieldInfo]) -> bool | None:
    states = [_holds(c, doc_fields, fields.get(c.field)) for c in node.conditions]
    if any(s is False for s in states):
        return False
    return True if states and all(s is True for s in states) else None


def conditions_hold(node: LearnedNode, doc_fields: dict[str, str], fields: dict[str, FieldInfo]) -> bool:
    """Is the block included for these facts? True if it has no conditions or all hold; a missing value means no."""
    return not node.conditions or _conditions_state(node, doc_fields, fields) is True


# the facts schema: which inputs a document cannot be written without


def required_inputs(tree: LearnedTree) -> set[str]:
    """The fields that a document cannot be written without.

    A field is required if some block that is always included (no condition on it or above it) and is not
    `generated` has the field in every one of its wordings (the text and each alternative): there is no wording of
    that block that gets by without it. A field that is only in some wordings (the interest start date, when another
    wording says "at the statutory rate"), only in conditional blocks or generated blocks, or only in a condition
    (a flag), is optional.
    """
    required: set[str] = set()

    def visit(node: LearnedNode) -> None:
        if node.conditions:
            return  # this block and everything under it is optional
        if node.kind != "generated":
            wordings = [set(find_spans(t)) for t in [node.text, *node.alternatives]]
            required.update(set.intersection(*wordings))
        for child in node.children:
            visit(child)

    for root in tree.nodes:
        visit(root)
    return required


def derive_facts_schema(tree: LearnedTree) -> FactsSchema:
    """The inputs of generation: the tree's described fields without the calculated ones, each marked required or not."""
    needed = required_inputs(tree)
    return FactsSchema(
        category=tree.category,
        fields=[InputField(**f.model_dump(), required=f.name in needed) for f in tree.fields if not f.calculated],
    )


# conformance: does the source document parse against the tree?


def _render(text: str, doc_fields: dict[str, str]) -> str:
    return SPAN_RE.sub(lambda m: doc_fields.get(m.group(1), m.group(0)), text)


def _normalize(text: str) -> str:
    return LEADING_NUMBER.sub("", re.sub(r"\s+", " ", text).strip())


def _node_matches(learned: LearnedNode, fact: FactNode, doc_fields: dict[str, str]) -> bool:
    if learned.type != fact.type:
        return False
    if learned.kind == "generated":
        return True  # varies with the case: any text is acceptable, so it cannot be verified
    wanted = _normalize(_render(fact.text, doc_fields))
    return any(_normalize(_render(t, doc_fields)) == wanted for t in [learned.text, *learned.alternatives])


def _match_children(
    learned: Sequence[LearnedNode],
    facts: Sequence[FactNode],
    doc_fields: dict[str, str],
    fields: dict[str, FieldInfo],
    mismatches: list[str],
    where: str,
) -> int:
    """Match a document's blocks, in order, against the tree's blocks; return how many blocks matched."""

    def skipped(node: LearnedNode) -> None:
        if not node.conditions:
            mismatches.append(f"{where}: required block missing: {node.text[:60]!r}")
        elif _conditions_state(node, doc_fields, fields) is True:
            mismatches.append(f"{where}: block missing although its condition holds: {node.text[:60]!r}")

    matched = 0
    i = 0
    for fact in facts:
        found = next((j for j in range(i, len(learned)) if _node_matches(learned[j], fact, doc_fields)), None)
        if found is None:
            mismatches.append(f"{where}: no block matches {fact.text[:60]!r}")
            continue
        for node in learned[i:found]:
            skipped(node)
        i = found + 1
        node = learned[found]
        if node.conditions and _conditions_state(node, doc_fields, fields) is False:
            mismatches.append(f"{where}: block present although its condition does not hold: {node.text[:60]!r}")
        matched += 1 + _match_children(node.children, fact.children, doc_fields, fields, mismatches, node.text[:40])
    for node in learned[i:]:
        skipped(node)
    return matched


def check_conformance(tree: LearnedTree, facts: DocumentFacts) -> DocumentConformance:
    """Parse a source document against the tree: every block must match a learned block (same type, same text
    once its own field values are filled in, or one of the alternatives; a `generated` block accepts any
    text), in order, and the blocks the document lacks must be conditional."""
    fields = {f.name: f for f in tree.fields}
    mismatches: list[str] = []
    matched = _match_children(tree.nodes, facts.nodes, facts.fields, fields, mismatches, "top level")
    return DocumentConformance(
        document_id=facts.document_id,
        matched=matched,
        total=len(list(walk(facts.nodes))),
        mismatches=mismatches,
    )


# assembly


def assemble(aligned: Sequence[AlignedUnit], reconciled: ReconciledAnalysis, category: str) -> Assembly:
    """Stitch the units into the learned tree, check it, derive the facts schema and check conformance.

    Hard problems raise `LearningError`: a gap in the unit positions, a node type that is not in the reconciled
    template, an illegal name, a used name that no unit describes, or a document below `MIN_CONFORMANCE`.
    Soft problems are logged and returned in `warnings`.
    """
    if not aligned:
        raise LearningError("Nothing to assemble: there are no aligned units")
    units = sorted(aligned, key=lambda u: u.position)
    if [u.position for u in units] != list(range(1, len(units) + 1)):
        raise LearningError(f"The unit positions must be 1 to {len(units)} without gaps, got {[u.position for u in units]}")
    logger.info("assembly_started units=%d documents=%d category=%s", len(units), len(reconciled.facts), category)

    nodes = [u.node for u in units]
    all_nodes = list(walk(nodes))
    warnings: list[str] = []

    # merge the field descriptions of the units: the first description of a field wins
    merged: dict[str, FieldInfo] = {}
    for unit in units:
        for info in unit.fields:
            first = merged.setdefault(info.name, info)
            if first is not info and first.type != info.type:
                warnings.append(f"field '{info.name}' is described as {first.type} and as {info.type}; using {first.type}")
            if first is not info and first.calculated != info.calculated:
                warnings.append(f"field '{info.name}' is marked calculated in only one unit; using {first.calculated}")

    used: list[str] = []  # in order of first use
    for node in all_nodes:
        for name in [*find_spans(node.text), *(s for alt in node.alternatives for s in find_spans(alt)),
                     *(c.field for c in node.conditions)]:
            if name not in used:
                used.append(name)

    # hard checks
    problems = []
    template_types = {e.name for e in reconciled.template.element_types}
    unknown_types = sorted({n.type for n in all_nodes} - template_types)
    if unknown_types:
        problems.append(f"node types that are not in the reconciled template: {unknown_types}")
    illegal = sorted(n for n in {*used, *merged} if not LEGAL_NAME.match(n))
    if illegal:
        problems.append(f"illegal names (use snake_case): {illegal}")
    if problems:
        raise LearningError("Assembly failed: " + "; ".join(problems))

    # soft checks
    for node in all_nodes:
        for condition in node.conditions:
            info = merged[condition.field]
            if condition.op in ORDERING_OPS and info.type not in ("number", "date"):
                warnings.append(f"condition {condition.field} {condition.op} {condition.value}: ordering needs a number or date field, not {info.type}")
            if not _value_fits(info.type, condition.value):
                warnings.append(f"condition {condition.field} {condition.op} {condition.value}: the value is not a valid {info.type}")

    ordered = [merged[n] for n in used] + [f for n, f in merged.items() if n not in used]
    tree = LearnedTree(category=category, nodes=nodes, fields=ordered)
    schema = derive_facts_schema(tree)

    conformance = [check_conformance(tree, facts) for facts in reconciled.facts]
    for result in conformance:
        logger.info(
            "conformance document_id=%s score=%.2f matched=%d total=%d", result.document_id, result.score,
            result.matched, result.total,
        )
        for mismatch in result.mismatches[:10]:
            logger.warning("conformance_mismatch document_id=%s %s", result.document_id, mismatch)
    for warning in warnings:
        logger.warning("assembly_warning %s", warning)

    low = [r for r in conformance if r.score < MIN_CONFORMANCE]
    if low:
        details = "; ".join(f"{r.document_id} {r.score:.0%} (e.g. {r.mismatches[0] if r.mismatches else 'n/a'})" for r in low)
        raise LearningError(f"Documents do not conform to the learned tree (below {MIN_CONFORMANCE:.0%}): {details}")

    logger.info(
        "assembly_finished units=%d fields=%d schema_fields=%d warnings=%d min_conformance=%.2f",
        len(units), len(ordered), len(schema.fields), len(warnings), min((r.score for r in conformance), default=1.0),
    )
    return Assembly(tree=tree, facts_schema=schema, warnings=warnings, conformance=conformance)
