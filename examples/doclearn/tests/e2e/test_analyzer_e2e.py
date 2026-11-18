"""End-to-end tests: the real agents against real Gemini, on the dummy documents in tests/fixtures.

Run them with `make e2e` (needs GOOGLE_API_KEY in .env). They cost money, so they are not part of
`make test`. They test the agents only, one model call per test, to keep the number of calls low:
fan-out and pipelines are covered by the mocked tests. They print a short report; `make e2e` shows it.
(The API-key skip, the logging and the `e2e` marker come from tests/e2e/conftest.py.)
"""

import re
from pathlib import Path

import pytest

from doclearn.analyzer import AnalyzerInput, DocumentAnalyzer
from doclearn.cli import read_documents
from doclearn.facts import SPAN_RE, heading_key, walk
from doclearn.models import Document
from micro.models import RunStatus
from micro.runner import AgentRunner

FIXTURES = Path(__file__).parent.parent / "fixtures"
MIN_VERBATIM = 0.8  # share of nodes whose text (spans filled in) is found verbatim in the source


@pytest.fixture
def runner(db):
    return AgentRunner(db)  # the real model


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def rendered(node, fields: dict[str, str]) -> str:
    return SPAN_RE.sub(lambda m: fields.get(m.group(1), m.group(0)), node.text)


def verbatim_misses(facts, source: str) -> tuple[float, list[str]]:
    """The share of nodes found verbatim in the source (spans filled in), and the ones that were not."""
    source = normalize(source)
    nodes = list(walk(facts.nodes))
    misses = [n.text for n in nodes if normalize(rendered(n, facts.fields)) not in source]
    return 1 - len(misses) / max(len(nodes), 1), misses


def depth(nodes, level=1) -> int:
    return max([level] + [depth(n.children, level + 1) for n in nodes if n.children])


def report(title: str, facts, template) -> None:
    nodes = list(walk(facts.nodes))
    print(f"\n[{title}] {facts.document_id}: {len(nodes)} nodes ({len(facts.nodes)} top-level, nesting depth {depth(facts.nodes)}), {len(facts.fields)} fields")
    print("  element types:", ", ".join(f"{e.name}{'*' if e.repeatable else ''}" for e in template.element_types))


def assert_values_come_from_the_source(facts, source: str) -> None:
    text = normalize(source)
    invented = [(k, v) for k, v in facts.fields.items() if normalize(v) not in text]
    assert not invented, f"{facts.document_id}: field values not found in the document: {invented}"


async def check_analyzer(
    db, runner, doc: Document, category: str, reference: str, names: tuple[str, ...], top_level: list[str]
) -> None:
    """Run the real Document Analyzer on one document (one model call) and check what it returned and what was logged."""
    job_id = await db.create_job("e2e")

    result = await runner.run(DocumentAnalyzer, AnalyzerInput(document=doc, category=category), job_id=job_id)

    assert result.status == RunStatus.SUCCESS, result.error
    facts = result.output.to_facts(doc.document_id)
    template = result.output.to_template(category)
    report(category, facts, template)
    print(f"  tokens in/out: {result.input_tokens}/{result.output_tokens}, model: {result.model_name}, attempts: {result.attempts}")

    assert len(list(walk(facts.nodes))) >= 10
    assert len(template.element_types) >= 4

    # the top level is the title and the sections, nothing else (no divider, metadata or signature line left loose),
    # so that learning needs no code to cut the document into sections
    top = [(n.type, heading_key(n.text)) for n in facts.nodes]
    print(f"  top level: {top}")
    assert [key for _, key in top] == top_level, f"top-level blocks {top}"
    assert top[0][0] == "title" and all(t == "section" for t, _ in top[1:]), f"top-level types {top}"
    assert not [c for c in facts.nodes[0].children if c.type == "section"], "the title contains a section"

    # the hierarchy, which every later step depends on: a flat list (a real run returned depth 1 and no children)
    # would make every line its own unit. Sections must contain their content.
    assert depth(facts.nodes) >= 3, f"no hierarchy: nesting depth {depth(facts.nodes)}"
    sections = [n for n in facts.nodes if n.text.lstrip().startswith("##")]
    assert sections and all(n.children for n in sections), "a ## section has no children"
    assert_values_come_from_the_source(facts, doc.text)

    # the case-specific values became fields; the reference number was not left behind as literal text
    for expected in (reference, *names):
        assert any(expected in value for value in facts.fields.values()), f"{expected!r} is not in the fields"
    assert reference not in " ".join(n.text for n in walk(facts.nodes))

    ratio, misses = verbatim_misses(facts, doc.text)
    print(f"  verbatim: {ratio:.0%}" + (f", not found verbatim: {misses[:3]}" if misses else ""))
    assert ratio >= MIN_VERBATIM

    row = await db.get_run(result.agent_run_id)
    assert row.status == RunStatus.SUCCESS and row.input_tokens and row.output_tokens
    assert row.reported_model_name and row.system_prompt_hash and row.completed_at


async def test_analyzer_on_a_project_proposal(db, runner):
    await check_analyzer(
        db, runner, read_documents(FIXTURES / "proposals")[0], "project_proposals", "PRP-FX-101",
        ("Quillfeather Books Ltd", "Copperline Software Ltd"),
        ["project proposal", "client", "vendor", "proposal", "supporting material", "closing"],
    )


async def test_analyzer_on_an_incident_report(db, runner):
    await check_analyzer(
        db, runner, read_documents(FIXTURES / "incidents")[0], "incident_reports", "INC-FX-201",
        ("Greta Holm", "Product Search"),
        ["incident report", "reporter", "affected service", "incident summary", "follow-up", "sign-off"],
    )
