from doclearn.models import DocumentTemplate, ElementType, FactsSchema, FieldInfo, InputField, LearnedNode, LearnedTree
from doclearn.store import LearnedStore

from micro.db import Database


def artifacts(category="proposals", marker="x"):
    template = DocumentTemplate(
        category=category, element_types=[ElementType(name="section", markdown="## {{h}}", repeatable=True)]
    )
    tree = LearnedTree(
        category=category,
        nodes=[
            LearnedNode(
                type="section",
                text=f"## {marker} {{{{client_name}}}}",
                kind="slot",
                alternatives=["## alt"],
                children=[LearnedNode(type="statement", text="s", kind="fixed")],
            )
        ],
        fields=[FieldInfo(name="client_name", type="text", description="Client", calculated=False)],
    )
    schema = FactsSchema(category=category, fields=[InputField(name="client_name", type="text", description="Client")])
    return template, tree, schema


async def test_what_is_saved_comes_back_unchanged_with_or_without_a_job(db, store):
    template, tree, schema = artifacts()
    job_id = await db.create_job("learn")

    learned_id = await store.save(
        category="proposals", template=template, tree=tree, facts_schema=schema, job_id=job_id
    )
    loaded = await store.get(learned_id)

    assert loaded.learned_id == learned_id and loaded.job_id == job_id and loaded.category == "proposals"
    assert loaded.template == template and loaded.tree == tree and loaded.facts_schema == schema
    assert loaded.tree.nodes[0].children[0].text == "s" and loaded.tree.nodes[0].alternatives == ["## alt"]
    assert loaded.created_at

    no_job = await store.save(category="proposals", template=template, tree=tree, facts_schema=schema)
    assert (await store.get(no_job)).job_id is None


async def test_latest_is_the_newest_result_of_the_category_and_history_is_kept(db, store):
    assert await store.get("missing") is None and await store.latest("proposals") is None and await store.list() == []

    ids = []
    for marker in ("first", "second", "third"):
        template, tree, schema = artifacts(marker=marker)
        ids.append(await store.save(category="proposals", template=template, tree=tree, facts_schema=schema))
    other_template, other_tree, other_schema = artifacts("incident_reports", "other")
    await store.save(category="incident_reports", template=other_template, tree=other_tree, facts_schema=other_schema)

    latest = await store.latest("proposals")
    assert latest.learned_id == ids[2] and "third" in latest.tree.nodes[0].text
    assert (await store.latest("incident_reports")).category == "incident_reports"
    assert [d.learned_id for d in await store.list("proposals")] == ids  # oldest first, nothing overwritten
    assert len(await store.list()) == 4


async def test_the_data_survives_reopening_the_database_and_opening_the_store_twice_is_harmless(tmp_path):
    path = tmp_path / "persist.db"
    template, tree, schema = artifacts()
    async with Database(path) as db1:
        store1 = await LearnedStore.open(db1)
        learned_id = await store1.save(category="proposals", template=template, tree=tree, facts_schema=schema)
        again = await LearnedStore.open(db1)  # `open` is idempotent and keeps the data
        assert (await again.get(learned_id)).category == "proposals"
    async with Database(path) as db2:
        store2 = await LearnedStore.open(db2)
        assert (await store2.latest("proposals")).learned_id == learned_id
