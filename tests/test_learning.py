import json

import pytest

from fakes import Reactive, flat_blocks, runner_with, structured_reply
from doclearn.learning import analyze_documents
from doclearn.models import Document
from micro.models import RunStatus


def analyzer_reply(document_id: str):
    return structured_reply(
        "AnalyzerOutput",
        {
            "blocks": flat_blocks(
                [
                    {"type": "title", "text": "# PROJECT PROPOSAL"},
                    {"type": "metadata", "text": f"**Proposal No.:** {{{{proposal_no}}}} ({document_id})"},
                ]
            ),
            "fields": [{"name": "proposal_no", "value": f"PRP-{document_id}"}],
            "styles": [
                {"name": "title", "markdown": "# {{title}}"},
                {"name": "metadata", "markdown": "**{{label}}:** {{value}}"},
            ],
        },
    )


def respond(user_text, call_number):
    """Documents named `bad-*` always fail, the rest are analyzed."""
    document_id = json.loads(user_text)["document"]["document_id"]
    if document_id.startswith("bad"):
        return RuntimeError(f"cannot read {document_id}")
    return analyzer_reply(document_id)


def docs(*ids):
    return [Document(document_id=i, text=f"# text of {i}") for i in ids]


async def test_every_document_is_analyzed_and_results_keep_the_input_order(db):
    runner = runner_with(db, Reactive(respond=respond))

    analysis = await analyze_documents(runner, docs("cfd_001", "cfd_002", "cfd_003"), "project_proposals")

    assert analysis.failed == []
    assert [a.facts.document_id for a in analysis.analyzed] == ["cfd_001", "cfd_002", "cfd_003"]
    first = analysis.analyzed[0]
    assert first.facts.fields == {"proposal_no": "PRP-cfd_001"}
    assert first.template.category == "project_proposals"
    assert {e.name for e in first.template.element_types} == {"title", "metadata"}


async def test_a_failing_document_is_recorded_and_the_others_carry_on(db):
    runner = runner_with(db, Reactive(respond=respond))
    job_id = await db.create_job("learn")

    analysis = await analyze_documents(runner, docs("a", "bad-1", "b"), "proposals", job_id=job_id)

    assert [a.facts.document_id for a in analysis.analyzed] == ["a", "b"]
    assert [(f.document_id, f.error) for f in analysis.failed] == [("bad-1", "RuntimeError: cannot read bad-1")]

    runs = await db.list_runs(job_id=job_id)
    bad = [r for r in runs if r.input["document"]["document_id"] == "bad-1"]
    assert len(bad) == 3 and all(r.status == RunStatus.FAILED for r in bad)  # the failure is in the run log too


async def test_when_every_document_fails_or_there_are_none_the_result_says_so_without_raising(db):
    runner = runner_with(db, Reactive(respond=respond))

    analysis = await analyze_documents(runner, docs("bad-1", "bad-2"), "proposals")
    assert analysis.analyzed == []
    assert [f.document_id for f in analysis.failed] == ["bad-1", "bad-2"]

    empty = await analyze_documents(runner, [], "proposals")
    assert empty.analyzed == [] and empty.failed == []


async def test_duplicate_document_ids_are_rejected_before_anything_runs(db):
    with pytest.raises(ValueError, match="unique"):
        await analyze_documents(runner_with(db, Reactive(respond=respond)), docs("a", "a"), "proposals")
    assert await db.list_runs() == []
