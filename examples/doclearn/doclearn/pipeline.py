"""The learning pipeline: documents in, a learned document tree and its facts schema saved in the database."""

import logging
from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field

from doclearn.assembly import DocumentConformance, assemble
from doclearn.learning import FailedDocument, align_units, analyze_documents, reconcile
from doclearn.models import Document
from doclearn.store import LearnedStore
from featherweight import AgentRunner

logger = logging.getLogger(__name__)

Progress = Callable[[str], None]  # called with a short, human-readable line at each stage of a pipeline


class LearningResult(BaseModel):
    """What a learning run produced. The learned tree itself is in the database under `learned_id`."""

    learned_id: str
    conformance: list[DocumentConformance] = Field(default_factory=list, description="One per analyzed document.")
    warnings: list[str] = Field(default_factory=list, description="The soft checks of the assembly that did not pass.")
    skipped: list[FailedDocument] = Field(default_factory=list, description="Documents that could not be analyzed.")


async def learn_document_tree(
    runner: AgentRunner,
    store: LearnedStore,
    documents: Sequence[Document],
    category: str,
    *,
    job_id: str,
    progress: Progress | None = None,
) -> LearningResult:
    """Learn a template from example documents of one category and save it. Returns a `LearningResult`.

    Steps: analyze every document (a document that fails is skipped), reconcile the vocabulary, align each
    unit across the documents, assemble and check the tree, then save the reconciled template, the learned
    tree and the facts schema. Any hard failure raises `LearningError` (or the agent's own failure), so run it
    through `featherweight.jobs.run_job`, which marks the job FAILED with the error and saves nothing:

        outcome = await run_job(db, "learn", lambda job_id: learn_document_tree(
            runner, store, documents, category, job_id=job_id))
        learned = await store.get(outcome.result.learned_id)   # or store.latest(category)

    The result itself lives in the database. `progress`, if given, is called with a line at each stage. Skipped
    documents, warnings and the conformance scores are in the returned `LearningResult` and in the logs, and every
    agent attempt is in the run log.
    """
    say = progress or (lambda _line: None)
    logger.info("learning_started category=%s documents=%d job_id=%s", category, len(documents), job_id)

    say(f"Analyzing {len(documents)} documents")
    analysis = await analyze_documents(runner, documents, category, job_id=job_id)
    for failed in analysis.failed:
        say(f"Skipped {failed.document_id}: it could not be analyzed")
    say("Reconciling the vocabulary of the documents")
    reconciled = await reconcile(runner, analysis, category, job_id=job_id)
    say("Aligning the sections across the documents")
    aligned = await align_units(runner, reconciled, category, job_id=job_id)
    say("Assembling the tree and checking conformance")
    assembly = assemble(aligned, reconciled, category)

    learned_id = await store.save(
        category=category,
        template=reconciled.template,
        tree=assembly.tree,
        facts_schema=assembly.facts_schema,
        job_id=job_id,
    )
    logger.info(
        "learning_finished learned_id=%s category=%s analyzed=%d skipped=%d units=%d schema_fields=%d warnings=%d job_id=%s",
        learned_id,
        category,
        len(analysis.analyzed),
        len(analysis.failed),
        len(assembly.tree.nodes),
        len(assembly.facts_schema.fields),
        len(assembly.warnings),
        job_id,
    )
    return LearningResult(
        learned_id=learned_id, conformance=assembly.conformance, warnings=assembly.warnings, skipped=analysis.failed
    )
