import pytest

from learned_checks import shared_sections, structural_problems
from doclearn.models import Condition, Document, LearnedNode, LearnedTree

SECTIONS = ["# PROPOSAL", "## CLIENT", "## PRICING"]


def node(text, *children, type_="section", conditions=None):
    return LearnedNode(type=type_, text=text, kind="fixed", children=list(children), conditions=conditions or [])


def tree(*nodes):
    return LearnedTree(category="c", nodes=list(nodes), fields=[])


OPTIONAL = Condition(field="include_extra", op="=", value="true")


@pytest.mark.parametrize(
    "good",
    [
        tree(node("# PROPOSAL"), node("## CLIENT", node("**Name:** x", type_="statement")), node("## PRICING")),
        tree(node("# Proposal"), node("### 2. Client"), node("## **PRICING**")),  # without markdown numbering and case
        tree(node("# PROPOSAL"), node("## CLIENT", node("- extra", type_="statement", conditions=[OPTIONAL])), node("## PRICING")),
    ],
)
def test_a_tree_with_the_expected_units_and_no_repeats_has_no_problems(good):
    assert structural_problems(good, SECTIONS) == []


def test_the_real_failure_every_section_twice_and_a_flag_for_each_is_caught():
    """The tree of a real run: the title unit swallowed the whole document, units 2 and 3 repeated its sections,
    and the units were made conditional on invented flags."""
    flag = [Condition(field="include_client_section", op="=", value="true")]
    bad = tree(
        node("# PROPOSAL", node("## CLIENT"), node("## PRICING")),
        node("## CLIENT", conditions=flag),
        node("## PRICING", conditions=flag),
    )
    problems = structural_problems(bad, SECTIONS)
    assert any("is conditional (include_client_section = true)" in p for p in problems)
    assert any("appear more than once" in p and "client" in p and "pricing" in p for p in problems)


@pytest.mark.parametrize(
    "units",
    [
        [node("# PROPOSAL"), node("## PRICING")],  # missing
        [node("# PROPOSAL"), node("## CLIENT"), node("## PRICING"), node("## EXTRA")],  # extra
        [node("# PROPOSAL"), node("## PRICING"), node("## CLIENT")],  # reordered
    ],
)
def test_missing_extra_or_reordered_units_are_caught(units):
    assert structural_problems(tree(*units), SECTIONS)


def test_the_shared_sections_are_the_level_one_and_two_headings_every_document_has():
    docs = [
        Document(document_id="a", text="# PROPOSAL\n\n## CLIENT\n### I. FACTS\n## EVIDENCE\n## CONCLUSION\n"),
        Document(document_id="b", text="# Proposal\n\n## 1. Client\n### I. FACTS\n## CONCLUSION\n"),  # no evidence section
    ]
    assert shared_sections(docs) == ["# PROPOSAL", "## CLIENT", "## CONCLUSION"]
