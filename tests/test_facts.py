import pytest

from builders import facts, fnode
from doclearn.facts import MappingError, apply_mappings, derive_template, find_spans, split_conflicting_merges, walk
from doclearn.models import DocumentFacts


def proposal_facts(document_id="cfd_002", case_span="proposal_no", date_span="date_filed") -> DocumentFacts:
    """A header block, parties, then a proposal with numbered items and bullets."""
    return DocumentFacts(
        document_id=document_id,
        nodes=[
            fnode("title", "# PROJECT PROPOSAL"),
            fnode(
                "metadata",
                "**Proposal No.:** {{%s}}" % case_span,
                fnode("metadata_line", "**Date Filed:** {{%s}}" % date_span),
            ),
            fnode("party_client", "## CLIENT", fnode("statement", "**Name:** {{client_name}}")),
            fnode("party_vendor", "## VENDOR", fnode("statement", "**Name:** {{vendor_name}}")),
            fnode(
                "section",
                "### I. FACTUAL BACKGROUND",
                fnode(
                    "numbered_item",
                    "1. **Supply Agreement**",
                    fnode("bullet", "- On {{agreement_date}}, parties executed a Materials Supply Agreement."),
                    fnode("bullet", "- Total contract value: {{contract_value}}."),
                ),
                fnode("numbered_item", "2. **Breach of Contract**", fnode("bullet", "- Vendor delivered late.")),
            ),
            fnode("section", "### II. TECHNICAL APPROACH"),
        ],
        fields={
            case_span: "PRP-2024-002",
            date_span: "April 12, 2024",
            "client_name": "Alder Robotics Ltd",
            "vendor_name": "Birch Analytics Inc",
            "agreement_date": "February 1, 2024",
            "contract_value": "$420,000",
        },
    )


STYLES = {
    "title": "# {{title}}",
    "metadata": "**{{label}}:** {{value}}",
    "metadata_line": "**{{label}}:** {{value}}",
    "party_client": "## CLIENT",
    "party_vendor": "## VENDOR",
    "statement": "{{text}}",
    "section": "### {{heading}}",
    "numbered_item": "{{n}}. **{{heading}}**",
    "bullet": "- {{text}}",
}


# helpers


def test_find_spans_and_walk():
    assert find_spans("On {{ agreement_date }} and {{contract_value}}, {{agreement_date}}") == [
        "agreement_date",
        "contract_value",
        "agreement_date",
    ]
    assert find_spans("no spans here") == []
    assert [n.type for n in walk(proposal_facts().nodes)][:6] == [
        "title", "metadata", "metadata_line", "party_client", "statement", "party_vendor",
    ]  # fmt: skip


def test_derive_template_collects_the_unique_types_sorted_by_name_and_ignores_unused_styles():
    template = derive_template(proposal_facts(), {**STYLES, "footnote": "[^{{n}}]"}, category="project_proposals")

    assert template.category == "project_proposals"
    names = [e.name for e in template.element_types]
    assert names == sorted(set(names)) == sorted(STYLES)  # no "footnote": no node has that type
    assert {e.name: e.markdown for e in template.element_types} == STYLES
    assert derive_template(DocumentFacts(document_id="d"), {}, "c").element_types == []


def test_repeatable_means_more_than_one_node_of_the_type_under_the_same_parent():
    by_name = {e.name: e.repeatable for e in derive_template(proposal_facts(), STYLES, "c").element_types}

    assert by_name["section"] is True  # two at the top level
    assert by_name["numbered_item"] is True  # two under the same section
    assert by_name["bullet"] is True  # two under the first numbered item
    assert by_name["title"] is False
    assert by_name["party_client"] is False
    assert by_name["metadata_line"] is False
    # "statement" appears under two different parents, once each: not repeatable by the rule
    assert by_name["statement"] is False


def test_derive_template_needs_a_style_for_every_used_type():
    styles = {k: v for k, v in STYLES.items() if k not in {"bullet", "section"}}
    with pytest.raises(ValueError, match=r"\['bullet', 'section'\]"):
        derive_template(proposal_facts(), styles, "c")


# applying mappings


TYPE_MAPPING = {"party_client": ["client", "party_client"], "bullet": ["bullet_point"]}
FIELD_MAPPING = {"proposal_no": ["proposal_num", "proposal_number", "proposal_no"], "date_filed": ["filing_date"]}


def test_apply_mappings_renames_types_spans_and_fields_and_makes_documents_consistent():
    doc = proposal_facts("cfd_003", case_span="proposal_num", date_span="filing_date")
    doc.nodes[3].type = "client"
    doc.nodes[4].children[0].children[0].type = "bullet_point"
    before = doc.model_dump()

    out = apply_mappings(doc, TYPE_MAPPING, FIELD_MAPPING)

    assert doc.model_dump() == before  # the input is not modified
    assert out.document_id == "cfd_003"
    assert out.nodes[3].type == "party_client"
    assert out.nodes[4].children[0].children[0].type == "bullet"
    assert out.nodes[1].text == "**Proposal No.:** {{proposal_no}}"
    assert out.nodes[1].children[0].text == "**Date Filed:** {{date_filed}}"
    assert set(out.fields) == {"proposal_no", "date_filed", "client_name", "vendor_name", "agreement_date", "contract_value"}
    assert out.fields["proposal_no"] == "PRP-2024-002"

    other = apply_mappings(proposal_facts("b", case_span="proposal_number"), TYPE_MAPPING, FIELD_MAPPING)  # another raw name
    assert other.nodes[1].text == out.nodes[1].text and other.fields.keys() == out.fields.keys()


def test_unmapped_names_are_left_as_they_are_spans_are_normalised_and_equal_values_merge():
    doc = facts("d", fnode("title", "Hello {{ unknown_field }} and {{proposal_num}}"), unknown_field="x", proposal_num="1")
    out = apply_mappings(doc, {}, FIELD_MAPPING)
    assert out.nodes[0].text == "Hello {{unknown_field}} and {{proposal_no}}"
    assert out.fields == {"unknown_field": "x", "proposal_no": "1"}

    same = facts("d", proposal_num="PRP-1", proposal_no="PRP-1")  # two raw names, one value
    assert apply_mappings(same, {}, FIELD_MAPPING).fields == {"proposal_no": "PRP-1"}


@pytest.mark.parametrize(
    "doc,type_mapping,field_mapping,message",
    [
        (facts("doc-9", proposal_num="PRP-1", proposal_no="PRP-2"), {}, FIELD_MAPPING, "doc-9"),  # two values for one field
        (proposal_facts(), {}, {"proposal_no": ["proposal_num"], "region_no": ["proposal_num"]}, "proposal_num"),
        (proposal_facts(), {"party_client": ["client"], "customer": ["client"]}, {}, "client"),
    ],
)
def test_a_mapping_that_cannot_be_applied_is_an_error(doc, type_mapping, field_mapping, message):
    with pytest.raises(MappingError, match=message):
        apply_mappings(doc, type_mapping, field_mapping)


# a field has one value per document: merges that break that are split


@pytest.mark.parametrize(
    "docs,mapping,expected,notes",
    [
        (  # the canonical name stays with its own name
            [facts("a", delay_days="16 days", delay_duration="3 weeks"), facts("b", delay_days="36")],
            {"delay_duration": ["delay_days", "delay_duration"]},
            {"delay_duration": ["delay_duration"], "delay_days": ["delay_days"]},
            1,
        ),
        (  # only the clashing names of a larger group are separated: `other` has the same value as `name`
            [facts("a", name="Alpine", short="Alp", other="Alpine")],
            {"client": ["name", "short", "other"]},
            {"client": ["name", "other"], "short": ["short"]},
            1,
        ),
        (  # different documents, or the same value ("X  Y" and "X Y" are the same): stay merged
            [facts("a", proposal_no="PRP-1", region="Berlin"), facts("b", proposal_num="PRP-2"), facts("c", proposal_no="X  Y", proposal_ref="X Y")],
            {"proposal_number": ["proposal_no", "proposal_num", "proposal_ref"]},
            {"proposal_number": ["proposal_no", "proposal_num", "proposal_ref"]},
            0,
        ),
    ],
)
def test_names_with_different_values_in_one_document_are_split_apart(docs, mapping, expected, notes):
    before = {k: list(v) for k, v in mapping.items()}
    result, found = split_conflicting_merges(mapping, docs)
    assert result == expected and len(found) == notes
    assert mapping == before  # the input is not modified


def test_a_new_name_never_takes_the_place_of_another_canonical_name():
    docs = [facts("a", x="1", y="2")]
    result, _ = split_conflicting_merges({"x": ["x", "y"], "y": ["z"]}, docs)
    assert result == {"x": ["x"], "y_2": ["y"], "y": ["z"]}
