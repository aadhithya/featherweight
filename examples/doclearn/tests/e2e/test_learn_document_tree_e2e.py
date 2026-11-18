"""End-to-end test of the whole learning pipeline, `learn_document_tree`, with the real agents against real Gemini.

Analyzer (one call per document), Reconciliation Agent (one call), Semantic Alignment (one call per section),
then assembly, checks, the conformance round trip and storage: 3 + 1 + 6 = 10 model calls on the 3 fixture proposals
when nothing is retried (each failed attempt adds one). Run with `make e2e ARGS="-k learn_document_tree"` (needs
GOOGLE_API_KEY in .env). Not part of `make test`. It prints a report: the learned tree, the facts schema, and the
conformance scores. The shape of the tree is asserted (`learned_checks.structural_problems`): exactly the six
sections of the fixtures, none of them conditional, no heading twice.
"""

import logging
import re
from pathlib import Path

import pytest

from doclearn.cli import read_documents
from doclearn.facts import SPAN_RE, find_spans, heading_key, walk
from doclearn.models import Document, LearnedTree
from doclearn.pipeline import learn_document_tree
from doclearn.store import LearnedStore
from micro.db import Database
from micro.jobs import run_job
from micro.models import RunStatus
from micro.runner import AgentRunner
from learned_checks import shared_sections, structural_problems

CATEGORY = "project_proposals"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "proposals"
EXAMPLES = Path(__file__).parent.parent.parent / "samples" / "project_proposals"
INCIDENT_CATEGORY = "incident_reports"
INCIDENT_EXAMPLES = Path(__file__).parent.parent.parent / "samples" / "incident_reports"
MIN_UNITS = 4  # the fixture proposals have 6 sections (title, client, vendor, proposal, supporting material, closing)
MAX_UNITS = 20  # a flat Analyzer output once gave 76 units; units are sections now, so far fewer
MIN_SCHEMA_FIELDS = 5  # and about 30 fillable values each
# the sections all three fixture proposals have, in order: the learned tree must have exactly these units
SECTIONS = ["# PROJECT PROPOSAL", "## CLIENT", "## VENDOR", "## PROPOSAL", "## SUPPORTING MATERIAL", "## CLOSING"]


# the blocks that hold one case-specific value each in the fixture proposals: (label, the unit it is in, or None for anywhere)
ROLE_BLOCKS = {
    "proposal_number": ("**Proposal No.:**", None),
    "date_issued": ("**Issued:**", None),
    "valid_until": ("**Valid Until:**", None),
    "client": ("**Name:**", "client"),
    "vendor": ("**Name:**", "vendor"),
}


def check_role_slots(tree: LearnedTree) -> None:
    """In the fixture proposals every document has the proposal number, issue date and validity lines, and a name line
    under CLIENT and under VENDOR, each with a different value: in the learned tree each is one `slot` block (no
    alternatives, no condition) whose span name differs from the other four (reconciled to one name per role)."""
    names = {}
    for role, (label, unit) in ROLE_BLOCKS.items():
        scope = [n for n in tree.nodes if heading_key(n.text) == unit] if unit else tree.nodes
        blocks = [n for n in walk(scope) if label in n.text]
        assert len(blocks) == 1, f"{role}: {len(blocks)} blocks with {label!r}"
        (block,) = blocks
        assert block.kind == "slot" and not block.alternatives and not block.conditions, f"{role}: {block.kind} {block.text!r}"
        span = re.search(re.escape(label) + r"\s*" + SPAN_RE.pattern, block.text)
        assert span, f"{role}: no span after {label!r} in {block.text!r}"
        names[role] = span.group(1)
    assert len(set(names.values())) == len(names), f"different roles share a span name: {names}"
    print(f"  span names by role: {names}")


def check_fixture_proposals_tree(tree: LearnedTree) -> None:
    """Only proposals b and c have an Optional Support term, so the learned block is conditional; the payment line is
    worded differently in each proposal, so some block has alternatives."""
    nodes = list(walk(tree.nodes))
    conditioned = [n for n in nodes if n.conditions]
    covered = " ".join(m.text for c in conditioned for m in walk([c]))  # a condition covers the block and its children
    assert "Optional Support" in covered, f"no conditional Optional Support block; conditional: {covered!r}"
    assert any(n.alternatives for n in nodes), "no block has alternatives although the payment lines are worded differently"


async def learn_and_check(
    runner: AgentRunner, db: Database, documents: list[Document], caplog, sections: list[str] = SECTIONS,
    category: str = CATEGORY, fixture_proposals: bool = False,
) -> None:
    """Run `learn_document_tree` through `run_job` and check what ended up in the database. `fixture_proposals` adds the
    checks that depend on the content of tests/fixtures/proposals (roles, the conditional block, nothing unmapped)."""
    store = await LearnedStore.open(db)
    caplog.set_level(logging.INFO, logger="doclearn")

    outcome = await run_job(
        db, "learn", lambda job_id: learn_document_tree(runner, store, documents, category, job_id=job_id)
    )

    assert outcome.status == RunStatus.SUCCESS, outcome.error
    assert outcome.result.skipped == [], outcome.result.skipped  # every document was analyzed
    learned = await store.get(outcome.result.learned_id)
    assert learned is not None and learned.category == category and learned.job_id == outcome.job_id
    tree, schema, template = learned.tree, learned.facts_schema, learned.template

    kinds: dict[str, int] = {}
    nodes = list(walk(tree.nodes))
    for n in nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    print(f"\n[learned] {len(tree.nodes)} units, {len(nodes)} blocks {kinds}; template types: {sorted(e.name for e in template.element_types)}")
    for position, root in enumerate(tree.nodes, 1):
        print(f"  unit {position}: [{root.kind}] {root.text[:60]} ({len(list(walk([root])))} blocks)")
    conditional = [n for n in nodes if n.conditions]
    for n in conditional:
        print(f"  conditional: {n.text[:50]} IF " + " AND ".join(f"{c.field} {c.op} {c.value}" for c in n.conditions))
    print(f"[facts schema] {len(schema.fields)} inputs:")
    for f in schema.fields:
        print(f"  {f.name} ({f.type}): {f.description}")
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith(("conformance", "document_skipped", "assembly_warning", "reconciliation_unmapped")):
            print(f"  {message}")

    # the tree: units in position order, every type in the template, every used name described
    assert MIN_UNITS <= len(tree.nodes) <= MAX_UNITS, f"{len(tree.nodes)} units"
    problems = structural_problems(tree, sections)  # the shape: these units, none conditional, no heading twice
    assert not problems, "; ".join(problems)
    assert {n.type for n in nodes} <= {e.name for e in template.element_types}
    assert {s for n in nodes for text in [n.text, *n.alternatives] for s in find_spans(text)} <= {f.name for f in tree.fields}
    assert any(n.kind == "slot" for n in nodes) and any(n.kind == "fixed" for n in nodes), f"kinds: {kinds}"

    if fixture_proposals:
        check_role_slots(tree)
        check_fixture_proposals_tree(tree)
        assert not [r for r in caplog.records if r.getMessage().startswith("reconciliation_unmapped")], "the Reconciler left names unmapped"

    # the facts schema: inputs only, each described and typed, no calculated fields
    assert schema.category == category and len(schema.fields) >= MIN_SCHEMA_FIELDS, f"only {len(schema.fields)} inputs"
    assert all(f.description.strip() and f.type for f in schema.fields), "a field has no description or type"
    assert not any(f.calculated for f in schema.fields), "a calculated field is in the facts schema"
    assert len({f.name for f in schema.fields}) == len(schema.fields), "a field appears twice"

    # the run log and the job: nothing was left half done
    job = await db.get_job(outcome.job_id)
    assert (job.status, job.error) == (RunStatus.SUCCESS, None) and job.completed_at
    runs = await db.list_runs(job_id=outcome.job_id)
    successful = [r for r in runs if r.status == RunStatus.SUCCESS]
    by_agent = {a: sum(r.agent_name == a for r in successful) for a in {r.agent_name for r in runs}}
    assert all(r.input_tokens and r.output_tokens for r in successful), "a successful run has no token counts"
    print(f"[run log] model calls: {len(runs)} ({len(runs) - len(successful)} retried); successful by agent: {by_agent}")
    assert by_agent == {
        "document_analyzer": len(documents),
        "reconciliation_agent": 1,
        "semantic_alignment": len(tree.nodes),
    }, by_agent


async def test_learn_document_tree_on_the_fixture_proposals(db, caplog):
    await learn_and_check(AgentRunner(db), db, read_documents(FIXTURES), caplog, fixture_proposals=True)


async def test_learn_document_tree_on_the_example_proposals(db, caplog):
    """The proposals in `examples/`: does the learned template fit its sources? The units must be the sections all four
    documents share (computed from the documents, not written down here), and the report prints each document's
    conformance score and the blocks that do not fit."""
    documents = read_documents(EXAMPLES)
    sections = shared_sections(documents)
    print(f"\n[example proposals] {[d.document_id for d in documents]}; shared sections: {sections}")
    await learn_and_check(AgentRunner(db), db, documents, caplog, sections)


async def test_learn_document_tree_on_the_example_incident_reports(db, caplog):
    """The incident reports in `examples/`, the second category: the units must be the sections all four share, and the
    report prints each document's conformance score and the blocks that do not fit."""
    documents = read_documents(INCIDENT_EXAMPLES)
    sections = shared_sections(documents)
    print(f"\n[example incident reports] {[d.document_id for d in documents]}; shared sections: {sections}")
    await learn_and_check(AgentRunner(db), db, documents, caplog, sections, INCIDENT_CATEGORY)
