"""End-to-end test of the real Reconciliation Agent against real Gemini: one model call, on the real input of a full
learning run (3 analyzed proposals, 53 span names), built from the real Analyzer outputs with the current
`build_reconciler_input` (so it includes each name's `contexts`), so it needs no Analyzer calls. Run with
`make e2e ARGS="-k reconciliation"` (needs GOOGLE_API_KEY in .env); not part of `make test`.

The price lines are where the Analyzer's names drift (`discovery_design_cost`, `discovery_design_price` and
`discovery_and_design_price` are one fact), and where an earlier Reconciler went wrong on a real run: a hand-written
input with only easy drift (the reference number and party names) did not notice. The Reconciler's role in the whole
pipeline is also checked by the proposal-fixtures learning e2e.
"""

from pathlib import Path

from micro.models import RunStatus
from micro.runner import AgentRunner

REAL_INPUT = Path(__file__).parent.parent / "fixtures" / "reconciler_input_proposals.json"

# raw names that must end up under ONE standard name: the same thing written differently by the Analyzer
MUST_MERGE = [
    ["discovery_and_design_price", "discovery_design_cost", "discovery_design_price"],  # the price of the design phase
    ["development_cost", "development_price"],
    ["testing_and_deployment_price", "testing_deployment_cost", "testing_deployment_price"],
    ["first_year_support_licences_price", "support_licences_cost", "support_licences_price"],
    ["training_cost", "training_price"],
    ["vendor_phone", "vendor_tel"],
]
# raw names that must stay apart: different facts, although they sit in the same section and look alike
MUST_STAY_APART = [
    ["implementation_subtotal", "services_licences_subtotal"],
    ["total_price", "implementation_subtotal"],
    ["training_price", "support_licences_price"],
]


def check_reconciliation(output, field_names: set[str], type_names: set[str]) -> list[str]:
    """What a good reconciliation of the real input looks like; returns the problems found."""
    standard = {raw: m.canonical for m in output.field_mapping for raw in m.raw_names}
    mapped_types = {raw for m in output.type_mapping for raw in m.raw_names}
    problems = []
    if type_names - mapped_types:
        problems.append(f"unmapped types: {sorted(type_names - mapped_types)}")
    if field_names - standard.keys():
        problems.append(f"unmapped names: {sorted(field_names - standard.keys())}")
    if standard.keys() - field_names:
        problems.append(f"invented names: {sorted(standard.keys() - field_names)}")
    for group in MUST_MERGE:
        if len({standard.get(n) for n in group}) != 1:
            problems.append(f"should be one field: {group} -> {[standard.get(n) for n in group]}")
    for a, b in MUST_STAY_APART:
        if standard.get(a) == standard.get(b):
            problems.append(f"should be different fields: {a} and {b} -> both '{standard.get(a)}'")
    return problems


async def test_reconciliation_agent_on_the_real_input_of_a_full_run(db):
    from doclearn.reconciler import ReconcilerInput, ReconciliationAgent

    real = ReconcilerInput.model_validate_json(REAL_INPUT.read_text(encoding="utf-8"))
    job_id = await db.create_job("e2e")

    result = await AgentRunner(db).run(ReconciliationAgent, real, job_id=job_id)

    assert result.status == RunStatus.SUCCESS, result.error
    out = result.output
    print(
        f"\n[reconciliation, real input] fields {len(real.fields)} -> {len(out.field_mapping)}; types {len(out.type_mapping)}"
    )
    for m in out.field_mapping:
        if len(m.raw_names) > 1:
            print(f"  merged: {m.canonical} <- {m.raw_names}")
    type_names = {e.name for t in real.templates for e in t.element_types}
    problems = check_reconciliation(out, {f.name for f in real.fields}, type_names)
    assert not problems, "; ".join(problems)
