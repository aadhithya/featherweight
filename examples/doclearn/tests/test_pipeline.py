"""The whole learning pipeline with a mocked LLM: Analyzer, Reconciler, Semantic Alignment, assembly, storage."""

import pytest

from builders import make_respond
from fakes import Reactive, runner_with
from doclearn.models import Document
from doclearn.pipeline import learn_document_tree
from micro.jobs import run_job
from micro.models import RunStatus

DOCUMENTS = [Document(document_id=i, text=f"# text of {i}") for i in ("a", "b", "c")]


def learn(db, store, model, documents=DOCUMENTS, category="proposals", **runner_kwargs):
    runner = runner_with(db, model, **runner_kwargs)
    return run_job(db, "learn", lambda job_id: learn_document_tree(runner, store, documents, category, job_id=job_id))


async def test_the_whole_pipeline_learns_stores_and_marks_the_job_success(db, store):
    outcome = await learn(db, store, Reactive(respond=make_respond()))

    assert outcome.status == RunStatus.SUCCESS, outcome.error
    learned = await store.get(outcome.result.learned_id)
    assert learned.category == "proposals" and learned.job_id == outcome.job_id

    assert [n.text for n in learned.tree.nodes] == ["# PROPOSAL", "## CLIENT", "## PRICING"]
    assert [c.text for c in learned.tree.nodes[0].children] == ["**Proposal No.:** {{proposal_no}}"]
    pricing = learned.tree.nodes[2]
    assert [c.text for c in pricing.children] == ["- Total price: {{total_price}}.", "- Add the optional training package."]
    assert pricing.children[1].conditions[0].field == "include_training"
    assert {e.name for e in learned.template.element_types} == {"title", "metadata", "section", "statement"}
    assert [f.name for f in learned.facts_schema.fields] == ["proposal_no", "client_name", "total_price", "include_training"]
    assert "proposal_num" not in learned.tree.model_dump_json()  # the naming drift of document b is reconciled before alignment

    job = await db.get_job(outcome.job_id)
    assert (job.status, job.error) == (RunStatus.SUCCESS, None) and job.completed_at
    runs = await db.list_runs(job_id=outcome.job_id)
    assert sorted(r.agent_name for r in runs) == ["document_analyzer"] * 3 + ["reconciliation_agent"] + ["semantic_alignment"] * 3
    assert all(r.status == RunStatus.SUCCESS for r in runs)


async def test_the_result_carries_the_conformance_the_warnings_and_the_skipped_documents(db, store):
    outcome = await learn(db, store, Reactive(respond=make_respond(fail_analyzing=("c",))), max_attempts=1)

    assert outcome.status == RunStatus.SUCCESS, outcome.error  # a document that cannot be analyzed is skipped
    result = outcome.result
    assert [(c.document_id, c.score) for c in result.conformance] == [("a", 1.0), ("b", 1.0)]
    assert [f.document_id for f in result.skipped] == ["c"] and isinstance(result.warnings, list)
    assert await store.get(result.learned_id) is not None


async def test_progress_lines_come_in_pipeline_order(db, store):
    lines: list[str] = []
    runner = runner_with(db, Reactive(respond=make_respond()))
    outcome = await run_job(
        db, "learn", lambda job_id: learn_document_tree(runner, store, DOCUMENTS, "proposals", job_id=job_id, progress=lines.append)
    )

    assert outcome.status == RunStatus.SUCCESS, outcome.error
    assert [line.split()[0] for line in lines] == ["Analyzing", "Reconciling", "Aligning", "Assembling"]
    assert "3 documents" in lines[0]


@pytest.mark.parametrize(
    "respond_kwargs,attempts,error",
    [
        (dict(fail_analyzing=("a", "b", "c")), 1, "LearningError: No document could be analyzed"),
        (dict(fail_unit=3), 2, "Semantic Alignment failed for unit 3: RuntimeError: alignment down"),
        (dict(pricing_text="## SOMETHING ELSE"), 3, "do not conform to the learned tree"),
    ],
)
async def test_a_failing_pipeline_fails_the_job_and_saves_nothing(db, store, respond_kwargs, attempts, error):
    outcome = await learn(db, store, Reactive(respond=make_respond(**respond_kwargs)), max_attempts=attempts)

    assert outcome.status == RunStatus.FAILED and outcome.result is None
    assert error in outcome.error
    assert (await db.get_job(outcome.job_id)).error == outcome.error
    assert await store.list() == []
