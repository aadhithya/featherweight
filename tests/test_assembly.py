import pytest

from builders import cond, facts, fnode, info, lnode, reconciled, unit
from doclearn.assembly import MIN_CONFORMANCE, assemble, check_conformance, parse_date, parse_number
from doclearn.learning import AlignedUnit, LearningError
from doclearn.models import LearnedTree


# the proposal-shaped example used by most tests: title, client section, pricing section with an optional item


def learned_units() -> list[AlignedUnit]:
    return [
        unit(1, lnode("title", "# PROJECT PROPOSAL")),
        unit(2, lnode("section", "## CLIENT", "fixed", lnode("statement", "**Name:** {{client_name}}", "slot")), info("client_name", description="Client's name")),
        unit(
            3,
            lnode(
                "section", "## PRICING", "fixed",
                lnode("statement", "- Total price: {{total_price}}.", "slot"),
                lnode("numbered_item", "2. **Optional Support:**", "fixed", lnode("statement", "- Training for two days."),
                      conditions=[cond("include_optional_support", "=", "true")]),
                lnode("statement", "- Travel costs are billed at cost.", "fixed", alternatives=["- Reasonable travel costs are billed at cost."]),
            ),
            info("total_price", "number", calculated=True, description="Total"),
            info("include_optional_support", "boolean", description="Include the training package?"),
        ),
    ]  # fmt: skip


def doc(document_id, total="$100,000", support=False, fees="- Travel costs are billed at cost.", name=None):
    pricing = [fnode("statement", "- Total price: {{total_price}}.")]
    if support:
        pricing.append(fnode("numbered_item", "3. **Optional Support:**", fnode("statement", "- Training for two days.")))
    pricing.append(fnode("statement", fees))
    return facts(
        document_id,
        fnode("title", "# PROJECT PROPOSAL"),
        fnode("section", "## CLIENT", fnode("statement", "**Name:** {{client_name}}")),
        fnode("section", "## PRICING", *pricing),
        client_name=name or f"Client {document_id}",
        total_price=total,
    )


# stitching and the hard checks


def test_units_are_stitched_by_position_whatever_order_they_arrive_in():
    result = assemble(list(reversed(learned_units())), reconciled(doc("a")), "proposals")

    assert [n.text for n in result.tree.nodes] == ["# PROJECT PROPOSAL", "## CLIENT", "## PRICING"]
    assert result.tree.category == "proposals"


def _a_gap_in_the_positions(units):
    units[2].position = 4


def _illegal_name(units):
    units[1].node.children[0].text = "**Name:** {{Client Name}}"
    units[1].fields[0] = info("Client Name")


def _both_problems(units):
    units[2].fields[0].name = "Bad Name"


NO_NUMBERED_ITEM = ("title", "section", "statement")


@pytest.mark.parametrize(
    "mutate,types,message",
    [
        (_a_gap_in_the_positions, None, "without gaps"),
        (lambda units: units.clear(), None, "Nothing to assemble"),
        (lambda units: None, NO_NUMBERED_ITEM, r"not in the reconciled template: \['numbered_item'\]"),
        (_illegal_name, None, r"illegal names \(use snake_case\): \['Client Name'\]"),
        (_both_problems, NO_NUMBERED_ITEM, r"(?s)(?=.*not in the reconciled template)(?=.*illegal names)"),  # reported together
    ],
)
def test_a_hard_problem_is_an_error(mutate, types, message):
    units = learned_units()
    mutate(units)
    analysis = reconciled(doc("a"), types=types) if types else reconciled(doc("a"))
    with pytest.raises(LearningError, match=message):
        assemble(units, analysis, "proposals")


@pytest.mark.parametrize("name,legal", [("client_name", True), ("a1_b2", True), ("1abc", False), ("Client", False), ("a-b", False), ("a b", False), ("_x", False)])
def test_what_a_legal_name_is(name, legal):
    units = [unit(1, lnode("title", "x {{%s}}" % name, "slot"), info(name))]
    if legal:
        assemble(units, reconciled(facts("a", fnode("title", "x {{%s}}" % name), **{name: "v"})), "proposals")
    else:
        with pytest.raises(LearningError, match="illegal names"):
            assemble(units, reconciled(facts("a", fnode("title", "x"))), "proposals")


# fields and the facts schema


def test_the_tree_keeps_every_field_and_the_schema_only_the_inputs_in_order_of_first_use():
    units = learned_units()
    units[0].fields.append(info("case_background", description="What happened, for the generated narrative"))

    result = assemble(units, reconciled(doc("a")), "proposals")

    assert [f.name for f in result.tree.fields] == ["client_name", "total_price", "include_optional_support", "case_background"]
    # not the calculated total; a described field that no block uses is an input too, and comes last
    assert [f.name for f in result.facts_schema.fields] == ["client_name", "include_optional_support", "case_background"]
    assert result.facts_schema.category == "proposals"
    flag = result.facts_schema.fields[1]
    assert flag.type == "boolean" and flag.description == "Include the training package?"


def test_a_field_described_by_two_units_keeps_the_first_description_and_warns_on_a_conflict():
    units = learned_units()
    units[0].fields.append(info("client_name", "text", description="Another description"))
    units[1].fields.append(info("client_name", "text", description="x"))
    units[2].fields.append(info("client_name", "number"))

    result = assemble(units, reconciled(doc("a")), "proposals")

    described = [f for f in result.tree.fields if f.name == "client_name"]
    assert len(described) == 1 and described[0].description == "Another description"
    assert any("client_name" in w and "text and as number" in w for w in result.warnings)


# soft checks


def _calculated_in_one_unit_only(units):
    units[0].fields.append(info("total_price", "number", calculated=False))


def _condition(condition, *fields):
    def mutate(units):
        units[2].node.children[1].conditions = [condition]
        units[2].fields.extend(fields)

    return mutate


@pytest.mark.parametrize(
    "mutate,warning",
    [
        (_calculated_in_one_unit_only, "calculated"),
        (_condition(cond("jurisdiction", ">=", "Bayern"), info("jurisdiction", "text")), "ordering needs a number or date field, not text"),
        (_condition(cond("salary", ">=", "lots"), info("salary", "number")), "salary >= lots: the value is not a valid number"),
        (_condition(cond("filed", "<", "next week"), info("filed", "date")), "filed < next week: the value is not a valid date"),
        (_condition(cond("include_optional_support", "=", "maybe")), "include_optional_support = maybe: the value is not a valid boolean"),
        (_condition(cond("jurisdiction", "=", "Bayern"), info("jurisdiction", "text")), None),  # fine
    ],
)
def test_soft_problems_only_warn_and_the_assembly_still_completes(mutate, warning):
    units = learned_units()
    mutate(units)

    result = assemble(units, reconciled(doc("a")), "proposals")

    assert result.facts_schema.fields
    assert len(result.warnings) == (1 if warning else 0)
    assert warning is None or warning in result.warnings[0]


def test_values_and_dates_parse_the_way_the_documents_write_them():
    assert parse_number("$215,000") == 215000 and parse_number("42") == 42 and parse_number("-3.5") == -3.5
    assert parse_number("lots") is None
    assert str(parse_date("2025-05-20")) == "2025-05-20" and str(parse_date("May 20, 2025")) == "2025-05-20"
    assert str(parse_date("20.05.2025")) == "2025-05-20" and parse_date("soon") is None


# conformance


def tree_of(units=None) -> LearnedTree:
    return assemble(units or learned_units(), reconciled(doc("a")), "proposals").tree


def _generated_name_statement(units):
    units[1].node.children[0].kind = "generated"


def _condition_on_total(units):
    units[2].node.children[1].conditions = [cond("total_price", ">=", "500000")]


def _no_price_line():
    short = doc("e")
    short.nodes[2].children.pop(0)
    return short


def _extra_appendix():
    extra = doc("d")
    extra.nodes.append(fnode("section", "## APPENDIX"))
    return extra


def _swapped_sections():
    swapped = doc("f")
    swapped.nodes[1], swapped.nodes[2] = swapped.nodes[2], swapped.nodes[1]
    return swapped


def _generated_text():
    other = doc("g")
    other.nodes[1].children[0].text = "Something entirely different, written for this case."
    return other


@pytest.mark.parametrize(
    "mutate,document,mismatch",
    [
        (None, doc("a"), None),  # a document that the tree describes conforms fully
        (None, doc("b", name="Someone Else Ltd", total="$999,000"), None),  # slots compare after filling in the document's own values
        (None, doc("b", support=True, fees="- Reasonable travel costs are billed at cost."), None),  # an alternative wording; a conditional block that is present
        (None, doc("b", support=True), None),  # "3. **Optional Support:**" vs "2. ...": leading list numbers are ignored
        (None, doc("c", fees="- Award nothing at all."), "no block matches '- Award nothing at all.'"),  # but the rest of the text is not
        (None, _extra_appendix(), "APPENDIX"),  # a block the document has that the tree lacks
        (None, _no_price_line(), "required block missing: '- Total price: {{total_price}}.'"),
        (None, _swapped_sections(), ""),  # blocks out of order do not match
        (_generated_name_statement, _generated_text(), None),  # a generated block accepts any text of its type
        (_condition_on_total, doc("h", total="$800,000"), "missing although its condition holds"),
        (_condition_on_total, doc("i", total="$100,000"), None),  # the condition does not hold and the block is missing: fine
        (_condition_on_total, doc("j", total="$100,000", support=True), "present although its condition does not hold"),
        (_condition_on_total, doc("k", total="$800,000", support=True), None),
        (None, doc("l", support=False), None),  # a conditional block may be missing when its flag has no value
    ],
)
def test_check_conformance(mutate, document, mismatch):
    units = learned_units()
    if mutate:
        mutate(units)

    result = check_conformance(tree_of(units), document)

    if mismatch is None:
        assert result.mismatches == [] and result.score == 1.0
    else:
        assert any(mismatch in m for m in result.mismatches)
    if document.document_id in {"c", "f"}:  # changed text, swapped order
        assert result.score < 1.0
    counts = {"a": (6, 6), "d": (6, 7)}  # matched, total
    if document.document_id in counts:
        assert (result.matched, result.total) == counts[document.document_id]


# conformance inside the assembly


def test_assembly_reports_conformance_per_document_and_fails_below_the_threshold():
    result = assemble(learned_units(), reconciled(doc("a"), doc("b", support=True), doc("c", total="$5")), "proposals")
    assert [(c.document_id, c.score) for c in result.conformance] == [("a", 1.0), ("b", 1.0), ("c", 1.0)]

    almost = doc("m")
    almost.nodes[2].children.append(fnode("statement", "- An extra line nobody learned."))  # 6 of 7 blocks match
    report = assemble(learned_units(), reconciled(almost), "proposals").conformance[0]
    assert (report.matched, report.total) == (6, 7)
    assert report.score >= MIN_CONFORMANCE and report.mismatches  # just above the threshold: passes, mismatches reported

    bad = facts("bad", fnode("section", "## UNRELATED"), fnode("section", "## ALSO UNRELATED"))
    with pytest.raises(LearningError, match=r"below 80%\): bad 0% \(e\.g\."):
        assemble(learned_units(), reconciled(doc("a"), bad), "proposals")
