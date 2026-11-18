import json

import pytest
from doclearn.learning import AnalysisResult, AnalyzedDocument, LearningError, build_reconciler_input, reconcile
from doclearn.models import DocumentFacts, DocumentTemplate, ElementType, FactNode
from doclearn.reconciler import ReconcilerOutput
from fakes import Scripted, runner_with, structured_reply
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from micro.models import RunStatus


def analyzed(document_id, client_type, case_span, *, extra_type=None, client="Alder Ltd") -> AnalyzedDocument:
    nodes = [
        FactNode(type="title", text="# PROPOSAL"),
        FactNode(type="metadata", text=f"**Proposal No.:** {{{{{case_span}}}}}"),
        FactNode(type=client_type, text="## CLIENT", children=[FactNode(type="statement", text="{{client_name}}")]),
    ]
    if extra_type:
        nodes.append(FactNode(type=extra_type, text="## EXTRA"))
    facts = DocumentFacts(
        document_id=document_id,
        nodes=nodes,
        fields={case_span: f"PRP-{document_id}", "client_name": client},
    )
    types = sorted({n.type for n in nodes} | {"statement"})
    template = DocumentTemplate(
        category="proposals",
        element_types=[ElementType(name=t, markdown=f"md:{t}", repeatable=False) for t in types],
    )
    return AnalyzedDocument(facts=facts, template=template)


def three_documents() -> AnalysisResult:
    return AnalysisResult(
        analyzed=[
            analyzed("a", "party_client", "proposal_no"),
            analyzed("b", "client", "proposal_num", client="Bravo AG"),
            analyzed("c", "customer", "proposal_number", extra_type="appendix", client="Cargo SE"),
        ]
    )


def reconciler_output(**overrides) -> dict:
    out = {
        "type_mapping": [
            {"canonical": "title", "raw_names": ["title"]},
            {"canonical": "metadata", "raw_names": ["metadata"]},
            {"canonical": "party_client", "raw_names": ["party_client", "client", "customer"]},
            {"canonical": "statement", "raw_names": ["statement"]},
            {"canonical": "section", "raw_names": ["appendix"]},
        ],
        "field_mapping": [
            {"canonical": "proposal_no", "raw_names": ["proposal_no", "proposal_num", "proposal_number"]},
            {"canonical": "client_name", "raw_names": ["client_name"]},
        ],
        "element_types": [
            {"name": n, "markdown": f"md:{n}", "repeatable": False}
            for n in ("title", "metadata", "party_client", "statement", "section")
        ],
    }
    out.update(overrides)
    return out


def element_types(*names) -> list[dict]:
    return [{"name": n, "markdown": "m", "repeatable": False} for n in names]


def doc_with_lines(document_id: str, lines: dict[str, str], **fields) -> AnalyzedDocument:
    facts = DocumentFacts(
        document_id=document_id, nodes=[FactNode(type="title", text=t) for t in lines.values()], fields=fields
    )
    template = DocumentTemplate(category="c", element_types=[ElementType(name="title", markdown="#", repeatable=False)])
    return AnalyzedDocument(facts=facts, template=template)


# schema and validator


def test_a_consistent_output_is_valid_and_converts_to_maps_and_a_template():
    out = ReconcilerOutput.model_validate(reconciler_output())

    assert out.type_map()["party_client"] == ["party_client", "client", "customer"]
    assert out.field_map()["proposal_no"] == ["proposal_no", "proposal_num", "proposal_number"]
    template = out.to_template("proposals")
    assert template.category == "proposals"
    assert [e.name for e in template.element_types] == sorted(e.name for e in template.element_types)


@pytest.mark.parametrize(
    "overrides,message",
    [
        (
            dict(
                type_mapping=[
                    {"canonical": "title", "raw_names": ["title", "heading"]},
                    {"canonical": "section", "raw_names": ["heading"]},
                ],
                element_types=element_types("title", "section"),
            ),
            "type name 'heading' is mapped to both 'title' and 'section'",
        ),
        (
            dict(
                field_mapping=[
                    {"canonical": "proposal_no", "raw_names": ["proposal_no", "number"]},
                    {"canonical": "proposal_code", "raw_names": ["number"]},
                ]
            ),
            "field name 'number'",
        ),
        (
            dict(field_mapping=[{"canonical": "x", "raw_names": ["a"]}, {"canonical": "x", "raw_names": ["b"]}]),
            "field 'x' is listed twice",
        ),
        (dict(element_types=reconciler_output()["element_types"][:-1]), r"missing \['section'\]"),
        (
            dict(element_types=reconciler_output()["element_types"] + element_types("footer")),
            r"unexpected \['footer'\]",
        ),
        (dict(element_types=reconciler_output()["element_types"] * 2), "duplicate names"),
    ],
)
def test_invalid_reconciler_output_is_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        ReconcilerOutput.model_validate(reconciler_output(**overrides))


# building the input


def test_input_collects_templates_and_every_span_name_with_counts():
    inp = build_reconciler_input(three_documents(), "proposals")

    assert inp.category == "proposals"
    assert len(inp.templates) == 3
    by_name = {f.name: f for f in inp.fields}
    assert sorted(by_name) == ["client_name", "proposal_no", "proposal_num", "proposal_number"]
    assert by_name["client_name"].documents == 3
    assert by_name["proposal_no"].documents == 1


def test_every_name_comes_with_its_line_and_each_documents_value_and_an_unused_name_has_none():
    docs = AnalysisResult(
        analyzed=[
            doc_with_lines(
                "a",
                {
                    "item": "- Training and support fees: {{training_fees}}",
                    "sub": "- **Subtotal: {{subtotal}}**",
                    "who": "**Name:** {{client_name}}",
                },
                training_fees="$14,000",
                subtotal="$14,000",
                client_name="Alder Ltd",
                orphan="v",
            ),
            doc_with_lines("b", {"who": "**Name:** {{client_name}}"}, client_name="Bravo AG"),
        ]
    )
    by_name = {f.name: f for f in build_reconciler_input(docs, "c").fields}

    def seen(name):
        return [(c.line, c.value) for c in by_name[name].contexts]

    assert seen("training_fees") == [("- Training and support fees: {{training_fees}}", "$14,000")]
    assert seen("subtotal") == [("- **Subtotal: {{subtotal}}**", "$14,000")]
    assert seen("client_name") == [
        ("**Name:** {{client_name}}", "Alder Ltd"),
        ("**Name:** {{client_name}}", "Bravo AG"),
    ]
    assert by_name["orphan"].contexts == []


def test_contexts_are_distinct_capped_at_three_and_taken_across_documents():
    docs = AnalysisResult(
        analyzed=[
            doc_with_lines(
                str(i), {"x": f"line {i}: {{{{proposal_no}}}}", "y": "same line {{proposal_no}}"}, proposal_no="v"
            )
            for i in range(6)
        ]
    )
    contexts = {f.name: f for f in build_reconciler_input(docs, "c").fields}["proposal_no"].contexts
    assert [c.line for c in contexts] == [
        "line 0: {{proposal_no}}",
        "same line {{proposal_no}}",
        "line 1: {{proposal_no}}",
    ]  # distinct, in order, three


def test_a_long_line_is_cut_to_a_window_around_the_span_and_spaces_are_normalised():
    long = "Intro " * 40 + "the proposal of {{ client_name }}   is  founded " + "tail " * 40
    docs = AnalysisResult(analyzed=[doc_with_lines("a", {"x": long}, client_name="Alpine")])
    context = build_reconciler_input(docs, "c").fields[0].contexts[0].line

    assert "{{ client_name }}" in context and "  " not in context
    assert context.startswith("…") and context.endswith("…") and len(context) < len(long) / 2


# the pipeline step


async def test_mappings_are_applied_to_every_documents_facts_in_one_recorded_run(db):
    model = Scripted(script=[structured_reply("ReconcilerOutput", reconciler_output())])
    job_id = await db.create_job("learn")

    reconciled = await reconcile(runner_with(db, model), three_documents(), "proposals", job_id=job_id)

    assert [f.document_id for f in reconciled.facts] == ["a", "b", "c"]
    for facts in reconciled.facts:
        types = {n.type for n in facts.nodes}
        assert "client" not in types and "customer" not in types
        assert facts.nodes[1].text == "**Proposal No.:** {{proposal_no}}"
        assert set(facts.fields) == {"proposal_no", "client_name"}
    assert reconciled.facts[1].nodes[2].type == "party_client"
    assert reconciled.facts[2].nodes[3].type == "section"  # "appendix" merged into a similar type
    assert reconciled.facts[2].fields["proposal_no"] == "PRP-c"
    assert reconciled.template.category == "proposals"
    assert {e.name for e in reconciled.template.element_types} == {
        "title",
        "metadata",
        "party_client",
        "statement",
        "section",
    }
    assert reconciled.unmapped_types == [] and reconciled.unmapped_fields == []

    sent = json.loads([m for m in model.seen[0] if isinstance(m, HumanMessage)][0].content)  # what the agent was given
    assert len(sent["templates"]) == 3
    assert {f["name"] for f in sent["fields"]} == {"proposal_no", "proposal_num", "proposal_number", "client_name"}
    runs = await db.list_runs(job_id=job_id)
    assert [(r.agent_name, r.status) for r in runs] == [
        ("reconciliation_agent", RunStatus.SUCCESS)
    ]  # one call, not one per document


async def test_names_the_agent_leaves_out_stay_unchanged_and_are_reported(db):
    partial = reconciler_output(
        type_mapping=[
            {"canonical": "title", "raw_names": ["title"]},
            {"canonical": "metadata", "raw_names": ["metadata"]},
            {"canonical": "party_client", "raw_names": ["party_client", "client"]},
            {"canonical": "statement", "raw_names": ["statement"]},
        ],
        field_mapping=[{"canonical": "proposal_no", "raw_names": ["proposal_no", "proposal_num"]}],
        element_types=element_types("title", "metadata", "party_client", "statement"),
    )
    model = Scripted(script=[structured_reply("ReconcilerOutput", partial)])

    reconciled = await reconcile(runner_with(db, model), three_documents(), "proposals")

    assert reconciled.unmapped_types == ["appendix", "customer"]
    assert reconciled.unmapped_fields == ["client_name", "proposal_number"]
    assert reconciled.facts[2].nodes[2].type == "customer"  # unchanged
    assert "{{proposal_number}}" in reconciled.facts[2].nodes[1].text


@pytest.mark.parametrize(
    "analysis,replies,message",
    [
        (AnalysisResult(), [], "nothing to reconcile"),
        (three_documents(), [structured_reply("ReconcilerOutput", {})], "no element types"),
    ],
)
async def test_a_reconciliation_that_cannot_proceed_is_a_learning_error(db, analysis, replies, message):
    model = Scripted(script=replies)
    with pytest.raises(LearningError, match=message):
        await reconcile(runner_with(db, model), analysis, "proposals")
    assert model.calls == len(replies)  # nothing to reconcile: the agent is not even called


async def test_an_agent_that_keeps_failing_is_a_learning_error_and_the_attempts_are_recorded(db):
    model = Scripted(script=[RuntimeError("down")] * 3)
    job_id = await db.create_job("learn")

    with pytest.raises(LearningError, match="Reconciliation Agent failed: RuntimeError: down"):
        await reconcile(runner_with(db, model), three_documents(), "proposals", job_id=job_id)

    runs = await db.list_runs(job_id=job_id)
    assert [r.status for r in runs] == [RunStatus.FAILED] * 3


async def test_a_merge_of_names_with_different_values_in_one_document_is_split_not_a_failure(db):
    """A real run: the agent merged `delay_days` ("16 days") and `delay_duration` ("3 weeks"), both in one document; the
    mapping could not be applied and the job failed after the agent's 2 minute call. A field has one value per
    document, so the names stay apart (b has proposal_num and client_name, which cannot be one field)."""
    clash = reconciler_output(
        field_mapping=[
            {
                "canonical": "proposal_no",
                "raw_names": ["client_name", "proposal_no", "proposal_num", "proposal_number"],
            },
        ]
    )
    model = Scripted(script=[structured_reply("ReconcilerOutput", clash)])

    reconciled = await reconcile(runner_with(db, model), three_documents(), "proposals")

    assert reconciled.field_mapping == {
        "proposal_no": ["proposal_no", "proposal_num", "proposal_number"],
        "client_name": ["client_name"],
    }
    assert "client_name" in reconciled.facts[1].fields  # b keeps both values
