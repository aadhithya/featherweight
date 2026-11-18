"""The learning pipeline: the Analyzer, Reconciliation and Semantic Alignment steps and the code between them."""

import logging
import re
from collections import Counter
from collections.abc import Sequence
from statistics import fmean

from pydantic import BaseModel, Field

from doclearn.alignment import SemanticAlignmentAgent, SemanticAlignmentInput
from doclearn.analyzer import AnalyzerInput, DocumentAnalyzer
from doclearn.facts import (
    SPAN_RE,
    MappingError,
    apply_mappings,
    find_spans,
    heading_key,
    split_conflicting_merges,
    walk,
)
from doclearn.models import (
    Document,
    DocumentFacts,
    DocumentTemplate,
    FactNode,
    FieldContext,
    FieldExamples,
    FieldInfo,
    FieldValue,
    LearnedNode,
    UnitDocument,
)
from doclearn.reconciler import ReconcilerInput, ReconciliationAgent
from micro.models import RunStatus
from micro.runner import AgentRunner

logger = logging.getLogger(__name__)


class AnalyzedDocument(BaseModel):
    facts: DocumentFacts
    template: DocumentTemplate


class FailedDocument(BaseModel):
    document_id: str
    error: str


class AnalysisResult(BaseModel):
    analyzed: list[AnalyzedDocument] = Field(default_factory=list)  # in input order
    failed: list[FailedDocument] = Field(default_factory=list)


async def analyze_documents(
    runner: AgentRunner, documents: Sequence[Document], category: str, *, job_id: str | None = None
) -> AnalysisResult:
    """Run the Document Analyzer over every document concurrently and collect the results.

    A document whose analysis fails (after the runner's retries) is recorded in `failed` and skipped;
    the others carry on. The failed attempts are also in the run log. Deciding what to do when no
    document could be analyzed is left to the caller.
    """
    ids = [document.document_id for document in documents]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Document ids must be unique, got {ids}")

    results = await runner.run_many(
        DocumentAnalyzer,
        [AnalyzerInput(document=document, category=category) for document in documents],
        job_id=job_id,
    )

    analysis = AnalysisResult()
    for document, result in zip(documents, results, strict=True):
        if result.status == RunStatus.SUCCESS:
            facts = result.output.to_facts(document.document_id)
            analysis.analyzed.append(AnalyzedDocument(facts=facts, template=result.output.to_template(category)))
            logger.info(
                "document_analyzed document_id=%s top_level=%d blocks=%d fields=%d",
                document.document_id,
                len(facts.nodes),
                len(list(walk(facts.nodes))),
                len(facts.fields),
            )
        else:
            analysis.failed.append(FailedDocument(document_id=document.document_id, error=result.error or ""))
            logger.warning("document_skipped document_id=%s error=%s", document.document_id, result.error)

    logger.info(
        "analysis_finished category=%s analyzed=%d failed=%d job_id=%s",
        category,
        len(analysis.analyzed),
        len(analysis.failed),
        job_id,
    )
    return analysis


class LearningError(Exception):
    """A step of the learning pipeline cannot go on. The job runner marks the job FAILED."""


class ReconciledAnalysis(BaseModel):
    template: DocumentTemplate  # the reconciled template
    facts: list[DocumentFacts]  # every analyzed document, renamed to the canonical names, in input order
    type_mapping: dict[str, list[str]]
    field_mapping: dict[str, list[str]]
    unmapped_types: list[str] = Field(default_factory=list)  # raw names the agent left out; unchanged in the facts
    unmapped_fields: list[str] = Field(default_factory=list)


MAX_CONTEXTS = 3
CONTEXT_WINDOW = 80  # characters kept on each side of the span when a line is long


def _context(text: str, name: str) -> str:
    """The line where a span is used; a long line is cut to a window around the span."""
    text = " ".join(text.split())
    match = re.search(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", text)
    if match is None or len(text) <= 2 * CONTEXT_WINDOW + len(name) + 4:
        return text
    start, end = max(0, match.start() - CONTEXT_WINDOW), min(len(text), match.end() + CONTEXT_WINDOW)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def build_reconciler_input(analysis: AnalysisResult, category: str) -> ReconcilerInput:
    """The templates, and every span name across the documents with its document count and where it is used and what it held there."""
    documents: dict[str, int] = {}
    contexts: dict[str, list[FieldContext]] = {}
    for analyzed in analysis.analyzed:
        for node in walk(analyzed.facts.nodes):  # where each name is used and what it held there
            for name in dict.fromkeys(find_spans(node.text)):
                seen = contexts.setdefault(name, [])
                context = FieldContext(line=_context(node.text, name), value=analyzed.facts.fields.get(name, ""))
                if context not in seen and len(seen) < MAX_CONTEXTS:
                    seen.append(context)
        for name in analyzed.facts.fields:
            documents[name] = documents.get(name, 0) + 1
    return ReconcilerInput(
        category=category,
        templates=[analyzed.template for analyzed in analysis.analyzed],
        fields=[
            FieldExamples(
                name=n,
                documents=documents[n],
                contexts=contexts.get(n, []),
            )
            for n in sorted(documents)
        ],
    )


async def reconcile(
    runner: AgentRunner, analysis: AnalysisResult, category: str, *, job_id: str | None = None
) -> ReconciledAnalysis:
    """Run the Reconciliation Agent once over all analyzed documents, then apply its mappings to every
    document's facts so that all of them use the same type and field names.

    Raises `LearningError` if there is nothing to reconcile, if the agent fails after the runner's
    retries, or if its mappings cannot be applied.
    """
    if not analysis.analyzed:
        raise LearningError("No document could be analyzed, so there is nothing to reconcile")

    result = await runner.run(ReconciliationAgent, build_reconciler_input(analysis, category), job_id=job_id)
    if result.status != RunStatus.SUCCESS:
        raise LearningError(f"The Reconciliation Agent failed: {result.error}")
    output = result.output
    if not output.element_types:
        raise LearningError("The Reconciliation Agent returned no element types")

    type_map = output.type_map()
    field_map, splits = split_conflicting_merges(output.field_map(), [a.facts for a in analysis.analyzed])
    for note in splits:
        logger.warning("merge_split %s (two of these names have different values in one document)", note)
    try:
        facts = [apply_mappings(a.facts, type_map, field_map) for a in analysis.analyzed]
    except MappingError as exc:
        raise LearningError(f"The Reconciliation Agent's mappings cannot be applied: {exc}") from exc

    mapped_types = {raw for raws in type_map.values() for raw in raws}
    mapped_fields = {raw for raws in field_map.values() for raw in raws}
    unmapped_types = sorted({e.name for a in analysis.analyzed for e in a.template.element_types} - mapped_types)
    unmapped_fields = sorted({n for a in analysis.analyzed for n in a.facts.fields} - mapped_fields)
    if unmapped_types or unmapped_fields:
        logger.warning("reconciliation_unmapped types=%s fields=%s (left unchanged)", unmapped_types, unmapped_fields)
    logger.info(
        "reconciliation_finished documents=%d types=%d->%d fields=%d->%d unmapped_types=%d unmapped_fields=%d job_id=%s",
        len(facts),
        len({e.name for a in analysis.analyzed for e in a.template.element_types}),
        len(type_map),
        len({n for a in analysis.analyzed for n in a.facts.fields}),
        len(field_map),
        len(unmapped_types),
        len(unmapped_fields),
        job_id,
    )
    return ReconciledAnalysis(
        template=output.to_template(category),
        facts=facts,
        type_mapping=type_map,
        field_mapping=field_map,
        unmapped_types=unmapped_types,
        unmapped_fields=unmapped_fields,
    )


class AlignedUnit(BaseModel):
    position: int  # 1-based: the unit is the k-th section, sections being matched across documents by heading
    node: LearnedNode
    fields: list[FieldInfo] = Field(default_factory=list)


def build_units(reconciled: ReconciledAnalysis, category: str) -> list[SemanticAlignmentInput]:
    """One input per section: the top-level nodes of the documents (the Analyzer puts everything under the title or
    a section). The sections of different documents are matched by their heading text (`heading_key`; a heading used
    twice in a document is matched by occurrence), and the units are ordered by the average position of their sections.

    A document without a section is listed in `missing_in`. Only the spans used in the unit's subtree are passed,
    with their values, and only the styles of the types used.
    """
    styles = {e.name: e for e in reconciled.template.element_types}

    sections: dict[
        tuple[str, int], dict[str, tuple[int, FactNode]]
    ] = {}  # (heading, occurrence) -> document -> (index, node)
    for facts in reconciled.facts:
        seen: Counter[str] = Counter()
        for index, section in enumerate(facts.nodes):
            heading = heading_key(section.text)
            seen[heading] += 1
            sections.setdefault((heading, seen[heading]), {})[facts.document_id] = (index, section)

    ordered = sorted(sections.values(), key=lambda found: fmean(index for index, _ in found.values()))  # stable
    units = []
    for k, found in enumerate(ordered):
        documents: list[UnitDocument] = []
        missing: list[str] = []
        types_used: list[str] = []
        for facts in reconciled.facts:
            if facts.document_id not in found:
                missing.append(facts.document_id)
                continue
            root = found[facts.document_id][1]
            subtree = list(walk([root]))
            span_names = list(dict.fromkeys(name for node in subtree for name in find_spans(node.text)))
            documents.append(
                UnitDocument(
                    document_id=facts.document_id,
                    node=root,
                    fields=[FieldValue(name=n, value=facts.fields[n]) for n in span_names if n in facts.fields],
                )
            )
            types_used.extend(node.type for node in subtree)
        units.append(
            SemanticAlignmentInput(
                category=category,
                position=k + 1,
                documents=documents,
                missing_in=missing,
                styles=[styles[t] for t in sorted(set(types_used)) if t in styles],
            )
        )
    return units


def normalize_spans(node: LearnedNode, fields: list[FieldInfo]) -> tuple[LearnedNode, list[FieldInfo]]:
    """Use one span name per fact in a learned block and its alternatives, then drop the fields that nothing uses.

    The Reconciler can only merge names that mean the same everywhere; names for different facts stay apart
    (`travel_costs`, `licence_fees`, `hosting_costs`). The Semantic Alignment then puts them in
    one block as alternative wordings, each with its own span. In a block whose text has exactly one span, an
    alternative with exactly one span gets the block's name (several spans are left alone: which one is which is
    not clear). A field that was renamed away and is used nowhere else in the unit is dropped. Returns new objects.
    """
    renamed: set[str] = set()

    def visit(block: LearnedNode) -> LearnedNode:
        spans = find_spans(block.text)
        alternatives = block.alternatives
        if len(spans) == 1:
            target, normalized = spans[0], []
            for alternative in block.alternatives:
                found = find_spans(alternative)
                if len(found) == 1 and found[0] != target:
                    renamed.add(found[0])
                    alternative = SPAN_RE.sub("{{" + target + "}}", alternative)
                normalized.append(alternative)
            alternatives = [a for a in dict.fromkeys(normalized) if a != block.text]
        return block.model_copy(update={"alternatives": alternatives, "children": [visit(c) for c in block.children]})

    result = visit(node)
    still_used = {
        name
        for n in walk([result])
        for name in [
            *find_spans(n.text),
            *(s for a in n.alternatives for s in find_spans(a)),
            *(c.field for c in n.conditions),
        ]
    }
    dropped = renamed - still_used
    return result, [f for f in fields if f.name not in dropped]


async def align_units(
    runner: AgentRunner, reconciled: ReconciledAnalysis, category: str, *, job_id: str | None = None
) -> list[AlignedUnit]:
    """Run the Semantic Alignment agent on every unit concurrently, in position order.

    Raises `LearningError` if any unit still fails after the runner's retries, or returns no node:
    a missing unit would leave a hole in the tree, so the job fails (every agent output stays in the run
    log, so processing could be resumed).
    """
    units = build_units(reconciled, category)
    if not units:
        raise LearningError("There are no units to align: the documents have no content")
    logger.info("alignment_started units=%d documents=%d job_id=%s", len(units), len(reconciled.facts), job_id)

    results = await runner.run_many(SemanticAlignmentAgent, units, job_id=job_id)

    failures = [
        f"unit {unit.position}: {result.error if result.status != RunStatus.SUCCESS else 'no blocks returned'}"
        for unit, result in zip(units, results, strict=True)
        if result.status != RunStatus.SUCCESS or not result.output.blocks
    ]
    if failures:
        raise LearningError("Semantic Alignment failed for " + "; ".join(failures))

    aligned = []
    for unit, result in zip(units, results, strict=True):
        node, fields = normalize_spans(result.output.to_node(), result.output.fields)
        aligned.append(AlignedUnit(position=unit.position, node=node, fields=fields))
    logger.info(
        "alignment_finished units=%d fields=%d job_id=%s", len(aligned), sum(len(a.fields) for a in aligned), job_id
    )
    return aligned
