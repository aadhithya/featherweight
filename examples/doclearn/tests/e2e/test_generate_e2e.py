"""End-to-end test of the generation pipeline with the real agent against real Gemini: one Generator call (with
tool rounds for `check_document`, which checks the document in code), on a learned tree that was saved from a real
learning run on the four example proposals (`tests/fixtures/learned_proposals_sample.json`), so no learning calls are
made. Run with `make e2e ARGS="-k generate"` (needs GOOGLE_API_KEY in .env). It prints the document, the agent's notes
and the optional inputs that had no value.
"""

import json
import re
from pathlib import Path

from doclearn.assembly import derive_facts_schema
from doclearn.generation import check_generated, compile_tree, generate_document
from doclearn.models import DocumentTemplate, LearnedTree
from doclearn.store import LearnedStore

from micro.jobs import run_job
from micro.models import RunStatus
from micro.runner import AgentRunner

FIXTURE = Path(__file__).parent.parent / "fixtures" / "learned_proposals_sample.json"

# a new case: the 21 required inputs, and the optional ones that switch blocks on
CASE = {
    "proposal_no": "PRP-2025-0417",
    "issue_date": "June 3, 2025",
    "valid_until_date": "July 4, 2025",
    "client_name": "Greenfold Nurseries Ltd",
    "client_address": "7 Orchard Way, Norwich NR1 3JT, United Kingdom",
    "client_contact": "Operations Manager Alice Dunmore",
    "client_industry": "Horticulture",
    "vendor_name": "Tidewater Systems Ltd",
    "vendor_address": "Unit 9, Quay Park, Hull HU1 2BX, United Kingdom",
    "vendor_account_manager": "Imogen Pryce",
    "vendor_registration": "Company No. 08842110, England and Wales",
    "reference_client_1_name": "Fenland Salad Growers",
    "reference_client_1_description": "Greenhouse sensor platform for six sites",
    "vendor_tel": "+44 1482 555 0143",
    "vendor_email": "imogen.pryce@tidewater.example",
    "signatory_name": "Imogen Pryce",
    # the prices; the totals are calculated, not given
    "cost_discovery_design": "$12,000",
    "cost_development": "$85,000",
    "cost_testing_deployment": "$18,000",
    "cost_training": "$5,000",
    "cost_support_licences": "$14,000",
    # optional blocks switched on, each with its values
    "include_data_migration_cost": "true",
    "cost_data_migration": "$7,000",
    "include_hosting_setup_cost": "true",
    "cost_hosting_setup": "$3,000",
    "include_optional_support": "true",
    "optional_support_price": "$5,500",
}
EXPECTED_TOTAL = 144_000  # the six implementation and service prices of the case, plus data migration and hosting
GUIDANCE = (
    "Keep the tone formal. The project is a greenhouse climate monitoring platform: temperature and humidity sensors in "
    "four glasshouses, with alerts to the grower's phone."
)


async def test_generate_a_proposal_from_the_learned_tree(db, caplog):
    saved = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tree = LearnedTree.model_validate(saved["tree"])
    schema = derive_facts_schema(tree)
    store = await LearnedStore.open(db)
    learned_id = await store.save(
        category=tree.category,
        template=DocumentTemplate.model_validate(saved["template"]),
        tree=tree,
        facts_schema=schema,
    )
    learned = await store.get(learned_id)
    required = {f.name for f in schema.fields if f.required}
    assert required <= set(CASE), f"the case lacks required inputs: {sorted(required - set(CASE))}"

    runner = AgentRunner(db)
    outcome = await run_job(
        db, "generate", lambda job_id: generate_document(runner, learned, CASE, GUIDANCE, job_id=job_id)
    )
    assert outcome.status == RunStatus.SUCCESS, outcome.error
    document = outcome.result

    remaining = check_generated(
        compile_tree(tree, CASE), CASE, document.markdown
    )  # the same check the agent had as a tool
    print(
        f"\n[generated] problems left: {remaining}; not provided: {document.not_provided}; unused: {document.unused_inputs}"
    )
    print("[notes]", *document.notes, sep="\n  ")
    print("[document]\n" + document.markdown)

    text = document.markdown
    assert remaining == [], f"the check still finds problems: {remaining}"
    assert "{{" not in text
    for name in ("proposal_no", "client_name", "vendor_name", "vendor_email", "cost_development"):
        assert CASE[name] in text, f"{name} is not in the document"
    assert "Optional Support" in text and "Data migration" in text  # the flags were set
    assert not any(word in text.lower() for word in ("milk", "appointment", "vessel", "route sheets", "stock hub")), (
        "content of the example text leaked"
    )
    total_line = next((line for line in text.splitlines() if "TOTAL PRICE" in line.upper()), "")
    assert str(EXPECTED_TOTAL) in re.sub(r"\D", "", total_line), f"total: {total_line!r}, expected {EXPECTED_TOTAL}"
