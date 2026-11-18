import json

import pytest
from builders import block, facts, fld, fnode, info, lnode, reconciled
from doclearn.alignment import SemanticAlignmentAgent, SemanticAlignmentOutput
from doclearn.facts import walk
from doclearn.learning import LearningError, align_units, build_units, normalize_spans
from doclearn.models import Condition, LearnedNode
from fakes import Reactive, Scripted, runner_with, structured_reply
from pydantic import ValidationError

from micro.models import RunStatus


def three_documents():
    def doc(i, with_pricing_extra):
        pricing = [fnode("statement", "Total price: {{total_price}}.")]
        if with_pricing_extra:
            pricing.append(fnode("statement", "Include the optional training package."))
        return facts(
            f"d{i}",
            fnode("title", "# PROJECT PROPOSAL"),
            fnode("section", "## CLIENT", fnode("statement", "**Name:** {{client_name}}")),
            fnode("section", "## PRICING", *pricing),
            client_name=f"Client {i} Ltd",
            total_price=f"${i}00,000",
            unused="never in a subtree",
        )

    return reconciled(doc(1, False), doc(2, True), doc(3, False))


def learned(**overrides) -> dict:
    out = {
        "blocks": [
            block("b1", "", "section", "## PRICING"),
            block("b2", "b1", "statement", "Total price: {{total_price}}.", "slot"),
            block("b3", "b1", "statement", "Include the optional training package.", "generated",
                  conditions=[{"field": "include_training", "op": "=", "value": "true"}]),
        ],
        "fields": [
            fld("total_price", "number", "Total price in USD.", calculated=True),
            fld("include_training", "boolean", "Offer the optional training package?"),
        ],
    }  # fmt: skip
    out.update(overrides)
    return out


# models and validator


def test_a_consistent_output_rebuilds_the_tree_with_its_nesting_and_list_order():
    out = SemanticAlignmentOutput.model_validate(learned())
    root = out.to_node()

    assert isinstance(root, LearnedNode) and root.text == "## PRICING"
    assert [c.text for c in root.children] == [
        "Total price: {{total_price}}.",
        "Include the optional training package.",
    ]
    assert root.children[1].conditions == [Condition(field="include_training", op="=", value="true")]
    assert root.children[0].kind == "slot" and root.conditions == []
    assert out.open_questions == []
    with pytest.raises(ValueError, match="no blocks"):
        SemanticAlignmentOutput().to_node()

    blocks = [
        block("b1", "", "section", "S"),
        block("b2", "b1", "numbered_item", "1. A"),
        block("b3", "b2", "statement", "a1"),
        block("b4", "b2", "statement", "a2"),
        block("b5", "b1", "numbered_item", "2. B"),
        block("b6", "b5", "statement", "b1"),
    ]
    nested = SemanticAlignmentOutput.model_validate({"blocks": blocks}).to_node()
    assert [(c.text, [g.text for g in c.children]) for c in nested.children] == [
        ("1. A", ["a1", "a2"]),
        ("2. B", ["b1"]),
    ]


def _alternative_with_a_new_span(d):
    """A real run: three documents word a price item differently and each alternative kept its own span name
    (`lost_client_contract_value`, `client_penalties`, `charter_losses`), so one fact became three inputs. The
    validator does not excuse generated blocks: the alternatives must use the block's own name."""
    d["blocks"][2]["alternatives"] = ["Order {{other_party}} to replace the parts."]


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d.update(fields=d["fields"][1:]), r"fields used but not described: \['total_price'\]"),
        (lambda d: d.update(fields=d["fields"][:1]), "include_training"),  # a condition field
        (_alternative_with_a_new_span, "other_party"),
        (lambda d: d["fields"].append(d["fields"][0]), r"described twice: \['total_price'\]"),
        (lambda d: d["blocks"][2].update(id="b2"), r"block ids used twice: \['b2'\]"),
        (lambda d: d["blocks"][0].update(parent_id="b2"), "exactly one root block"),
        (lambda d: d["blocks"][2].update(parent_id=""), "exactly one root block"),
        (lambda d: d["blocks"][2].update(parent_id="b9"), r"unknown blocks: \['b9'\]"),
        (
            lambda d: (d["blocks"][1].update(parent_id="b3"), d["blocks"][2].update(parent_id="b2")),
            r"not connected to the root: \['b2', 'b3'\]",
        ),
        (
            lambda d: (d.update(fields=[]), d["blocks"][2].update(parent_id="b9")),
            r"(?s)(?=.*unknown blocks)(?=.*not described)",
        ),  # together
    ],
)
def test_invalid_alignment_output_is_rejected(mutate, message):
    data = learned()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        SemanticAlignmentOutput.model_validate(data)


def test_the_kind_is_read_off_the_text_not_given_by_the_agent():
    """A real run: 13 of 22 failed attempts contradicted the agent's own text (`slot` with no span, `fixed` with a
    span), e.g. for a block worded `... at the standard rate.` in one document and `... from {{date}}.` in two."""

    def kinds(data):
        return [n.kind for n in walk([SemanticAlignmentOutput.model_validate(data).to_node()])]

    assert kinds(learned()) == ["fixed", "slot", "generated"]  # no span, a span, judged generated

    data = learned()
    data["blocks"][1]["text"] = "Total price."  # no span: fixed, not an error
    data["fields"] = [f for f in data["fields"] if f["name"] != "total_price"]
    assert kinds(data)[1] == "fixed"

    data = learned()
    data["blocks"][1].update(
        text="Payment is due at the standard rate.", alternatives=["Payment is due from {{total_price}}."]
    )
    assert kinds(data)[1] == "slot"  # a span in any wording makes it a slot

    data = learned()
    data["blocks"][2].update(text="Order the Vendor to pay {{total_price}}.")
    assert kinds(data)[2] == "generated"  # the agent's judgement wins over the spans


# building the units


def test_one_unit_per_section_with_the_subtree_only_its_fields_and_only_its_styles():
    units = build_units(three_documents(), "proposals")

    assert [u.position for u in units] == [1, 2, 3]
    pricing = units[2]
    assert [d.document_id for d in pricing.documents] == ["d1", "d2", "d3"]
    assert pricing.missing_in == []
    assert [c.text for c in pricing.documents[1].node.children] == [
        "Total price: {{total_price}}.",
        "Include the optional training package.",
    ]
    assert [(f.name, f.value) for f in pricing.documents[1].fields] == [("total_price", "$200,000")]
    assert {f.name for d in units[1].documents for f in d.fields} == {"client_name"}  # "unused" is in no subtree
    assert units[0].documents[0].fields == []  # the title has no spans
    assert [s.name for s in units[0].styles] == ["title"]
    assert [s.name for s in pricing.styles] == ["section", "statement"] and pricing.styles[0].markdown == "md:section"


def test_edge_cases_of_the_units_no_documents_a_missing_section_and_a_span_without_a_value():
    assert build_units(reconciled(), "proposals") == []

    short = facts("short", fnode("title", "# PROJECT PROPOSAL"))
    units = build_units(reconciled(*three_documents().facts, short), "proposals")
    assert [u.missing_in for u in units] == [[], ["short"], ["short"]]
    assert [d.document_id for d in units[1].documents] == ["d1", "d2", "d3"]

    ghost = reconciled(facts("a", fnode("section", "x {{ghost}} {{real}}"), real="v"))
    assert [f.name for f in build_units(ghost, "c")[0].documents[0].fields] == ["real"]


# the coverage check (needs the input, so it is AgentBase.check_output)


def tree_of(n_blocks: int) -> SemanticAlignmentOutput:
    blocks = [block("b1", "", "section", "## S")] + [
        block(f"b{i + 2}", "b1", "statement", f"s{i}") for i in range(n_blocks - 1)
    ]
    return SemanticAlignmentOutput.model_validate({"blocks": blocks[:n_blocks]})


@pytest.mark.parametrize(
    "n_statements,n_returned,message",
    [
        (
            7,
            1,
            r"(?s)learned tree has 1 blocks but the largest document has 8.*Returned: \['b1<-root:## S'\]",
        ),  # only the heading
        (3, 1, "learned tree has 1 blocks"),
        (3, 2, None),  # the largest document has 4 blocks: exactly half is fine
        (3, 4, None),
        (3, 0, "no blocks"),
    ],
)
def test_the_coverage_check_rejects_a_tree_much_smaller_than_the_documents(n_statements, n_returned, message):
    docs = reconciled(
        facts("a", fnode("section", "## S", *[fnode("statement", f"s{i}") for i in range(n_statements)])),
        facts("b", fnode("section", "## S", fnode("statement", "s0"))),
    )
    unit = build_units(docs, "proposals")[0]
    output = tree_of(n_returned) if n_returned else SemanticAlignmentOutput()

    if message is None:
        SemanticAlignmentAgent().check_output(unit, output)
    else:
        with pytest.raises(ValueError, match=message):
            SemanticAlignmentAgent().check_output(unit, output)


# the pipeline step


def respond_by_position(user_text, call_number):
    payload = json.loads(user_text)
    position = payload["position"]
    if position == 3:
        return structured_reply("SemanticAlignmentOutput", learned())
    root_type = payload["documents"][0]["node"]["type"]  # a realistic reply uses the input's own element type
    return structured_reply(
        "SemanticAlignmentOutput", {"blocks": [block("b1", "", root_type, f"unit {position}")], "fields": []}
    )


async def test_every_unit_is_aligned_concurrently_and_returned_in_position_order(db):
    model = Reactive(respond=respond_by_position)
    job_id = await db.create_job("learn")

    aligned = await align_units(runner_with(db, model), three_documents(), "proposals", job_id=job_id)

    assert [a.position for a in aligned] == [1, 2, 3]
    assert [a.node.text for a in aligned[:2]] == ["unit 1", "unit 2"]
    assert aligned[2].node.children[1].conditions[0].field == "include_training"
    assert {f.name for f in aligned[2].fields} == {"total_price", "include_training"}
    assert model.max_in_flight > 1
    runs = await db.list_runs(job_id=job_id)
    assert [r.agent_name for r in runs] == ["semantic_alignment"] * 3 and all(
        r.status == RunStatus.SUCCESS for r in runs
    )


@pytest.mark.parametrize(
    "analysis,position,reply,message",
    [
        (three_documents(), 2, RuntimeError("down"), r"unit 2: RuntimeError: down"),
        (
            three_documents(),
            1,
            structured_reply("SemanticAlignmentOutput", {}),
            "unit 1: ValueError: no blocks returned",
        ),
        (reconciled(), None, None, "no units"),
    ],
)
async def test_a_unit_that_keeps_failing_fails_the_step_and_names_the_unit(db, analysis, position, reply, message):
    def respond(user_text, call_number):
        if json.loads(user_text)["position"] == position:
            return reply
        return respond_by_position(user_text, call_number)

    with pytest.raises(LearningError, match=message):
        await align_units(runner_with(db, Reactive(respond=respond)), analysis, "proposals")


@pytest.mark.parametrize(
    "first_reply,error",
    [
        (learned(fields=[]), None),  # spans not described: rejected when the reply is parsed, no failed run
        ({"blocks": [block("b1", "", "section", "## S")]}, "learned tree has 1 blocks"),  # only the heading
    ],
)
async def test_an_unacceptable_reply_is_retried_through_the_runner(db, first_reply, error):
    docs = reconciled(facts("a", fnode("section", "## S", *[fnode("statement", f"s{i}") for i in range(7)])))
    full = {
        "blocks": [block("b1", "", "section", "## S")]
        + [block(f"b{i + 2}", "b1", "statement", f"s{i}") for i in range(7)]
    }
    model = Scripted(
        script=[
            structured_reply("SemanticAlignmentOutput", first_reply),
            structured_reply("SemanticAlignmentOutput", full),
        ]
    )

    aligned = await align_units(runner_with(db, model), docs, "proposals")

    assert model.calls == 2 and len(aligned[0].node.children) == 7
    if error:  # rejected by the coverage check: the failed attempt is recorded
        failed = [r for r in await db.list_runs() if r.status == RunStatus.FAILED]
        assert len(failed) == 1 and error in failed[0].error


# the section split and the matching by heading


def section_doc(document_id, *headings, **fields):
    """A document whose top level is the given section headings, each with one statement."""
    return facts(document_id, *[fnode("section", h, fnode("statement", f"under {h}")) for h in headings], **fields)


def unit_headings(units):
    return [[d.node.text for d in u.documents] for u in units]


@pytest.mark.parametrize(
    "per_document,expected,missing_in",
    [
        (  # no evidence section in b: the rest does not shift
            [["## CLIENT", "## EVIDENCE", "## CONCLUSION"], ["## CLIENT", "## CONCLUSION"]],
            [["## CLIENT", "## CLIENT"], ["## EVIDENCE"], ["## CONCLUSION", "## CONCLUSION"]],
            [[], ["b"], []],
        ),
        (  # a heading used twice in one document is matched by occurrence
            [["## NOTES", "## PARTIES", "## NOTES"], ["## NOTES", "## PARTIES", "## NOTES"]],
            [["## NOTES"] * 2, ["## PARTIES"] * 2, ["## NOTES"] * 2],
            [[], [], []],
        ),
    ],
)
def test_sections_are_matched_by_heading_text_and_by_occurrence(per_document, expected, missing_in):
    docs = reconciled(*[section_doc(chr(ord("a") + i), *headings) for i, headings in enumerate(per_document)])
    units = build_units(docs, "proposals")

    assert unit_headings(units) == expected
    assert [u.missing_in for u in units] == missing_in


@pytest.mark.parametrize(
    "headings",
    [
        ["### IV. PRICING", "## 4.  Pricing", "## **pricing**"],  # numbering, case, spacing, markdown
        ["## PROPOSAL {{proposal_no}}", "## PROPOSAL {{proposal_number}}"],  # a span in the heading
    ],
)
def test_headings_match_whatever_their_markup_and_span_names(headings):
    docs = reconciled(*[section_doc(f"d{i}", h) for i, h in enumerate(headings)])
    units = build_units(docs, "proposals")
    assert len(units) == 1 and [d.document_id for d in units[0].documents] == [f"d{i}" for i in range(len(headings))]


def test_units_are_ordered_by_the_average_position_of_their_sections():
    docs = reconciled(
        section_doc("a", "## A", "## B", "## C", "## D"),
        section_doc("b", "## A", "## C", "## D"),
    )
    assert [u.documents[0].node.text for u in build_units(docs, "proposals")] == ["## A", "## B", "## C", "## D"]


def test_the_top_level_nodes_are_the_units_as_they_are_nothing_is_attached_in_code():
    """The Analyzer puts every other line under the title or a section; a loose top-level line is its own unit (it was
    attached to the section before it by a splitter in code, which is gone)."""
    docs = reconciled(
        facts("a", fnode("section", "## CLIENT", fnode("statement", "p")), fnode("divider", "---"), proposal_no="1"),
        facts("b", fnode("section", "## CLIENT", fnode("statement", "p")), fnode("divider", "---"), proposal_no="2"),
    )
    units = build_units(docs, "proposals")

    assert unit_headings(units) == [["## CLIENT", "## CLIENT"], ["---", "---"]]
    assert [c.text for c in units[0].documents[0].node.children] == ["p"]


# normalizing span names after the alignment (code, not the model)


def bullet(text, *children, kind="generated", alternatives=None, conditions=None):
    return lnode("bullet_point", text, kind, *children, alternatives=alternatives, conditions=conditions)


def test_an_alternative_with_another_span_name_gets_the_blocks_name_and_the_unused_field_goes():
    """A real run: three documents list their first price item under three names; each alternative kept its own
    span and one fact became three inputs."""
    item = bullet(
        "- Lost client contract: {{lost_client_contract_value}}",
        alternatives=[
            "- Client penalty payments: {{client_penalties}}",
            "- Lost charter contracts: {{charter_losses}}",
        ],
    )
    original = item.model_dump()
    node, fields = normalize_spans(
        bullet("## PRICES", item, kind="fixed"),
        [info(n, "number") for n in ("lost_client_contract_value", "client_penalties", "charter_losses")],
    )
    assert node.children[0].alternatives == [
        "- Client penalty payments: {{lost_client_contract_value}}",
        "- Lost charter contracts: {{lost_client_contract_value}}",
    ]
    assert [f.name for f in fields] == ["lost_client_contract_value"]
    assert item.model_dump() == original  # the input is not modified


def test_a_renamed_field_is_kept_when_used_elsewhere_and_ambiguous_alternatives_are_left_alone():
    first = bullet("- Item: {{amount}}", alternatives=["- Other: {{penalties}}"])
    second = bullet("- Penalties: {{penalties}}", kind="slot")
    flagged = bullet("- Extra", conditions=[Condition(field="extra", op="=", value="true")])
    node, fields = normalize_spans(
        bullet("## S", first, second, flagged, kind="fixed"), [info("amount"), info("penalties"), info("extra")]
    )
    assert node.children[0].alternatives == ["- Other: {{amount}}"]
    assert [f.name for f in fields] == ["amount", "penalties", "extra"]

    two = bullet("- On {{a}} {{b}}", alternatives=["- On {{c}}"])
    many = bullet("- On {{a}}", alternatives=["- On {{c}} to {{d}}"])
    node, fields = normalize_spans(bullet("## S", two, many, kind="fixed"), [info(n) for n in "abcd"])
    assert node.children[0].alternatives == ["- On {{c}}"] and node.children[1].alternatives == ["- On {{c}} to {{d}}"]
    assert len(fields) == 4

    repeated = bullet("- Fee: {{fee}}", alternatives=["- Fee: {{costs}}", "- Fee: {{costs}}", "- Other fee: {{costs}}"])
    node, _ = normalize_spans(repeated, [info("fee"), info("costs")])
    assert node.alternatives == ["- Other fee: {{fee}}"]  # one that becomes the text, and a repeat, are dropped


async def test_align_units_normalizes_the_spans_of_every_unit(db):
    def respond(user_text, call_number):
        data = learned()
        data["blocks"][1]["alternatives"] = ["Quote a price of {{amount_quoted}}."]
        data["fields"].append(fld("amount_quoted", "number"))
        return structured_reply("SemanticAlignmentOutput", data)

    one_section = facts(
        "d1", fnode("section", "## PRICING", fnode("statement", "Total price: {{total_price}}.")), total_price="$1"
    )
    aligned = await align_units(runner_with(db, Reactive(respond=respond)), reconciled(one_section), "proposals")

    assert aligned[0].node.children[0].alternatives == ["Quote a price of {{total_price}}."]
    assert "amount_quoted" not in [f.name for f in aligned[0].fields]
