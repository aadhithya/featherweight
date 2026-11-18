import pytest
from doclearn.analyzer import AnalyzerInput, AnalyzerOutput, DocumentAnalyzer
from doclearn.models import Document
from fakes import Scripted, flat_blocks, runner_with, structured_reply
from pydantic import ValidationError

from micro.models import RunStatus

NODES = [  # written nested for readability; the model returns it flat (see flat_blocks)
    {"type": "title", "text": "# PROJECT PROPOSAL"},
    {
        "type": "party_client",
        "text": "## CLIENT",
        "children": [
            {"type": "statement", "text": "**Name:** {{client_name}}"},
            {"type": "statement", "text": "**Region:** {{region}}"},
        ],
    },
]
BLOCKS = flat_blocks(NODES)
FIELDS = [{"name": "client_name", "value": "Alder Robotics Ltd"}, {"name": "region", "value": "Northern Region"}]
STYLES = [
    {"name": "title", "markdown": "# {{title}}"},
    {"name": "party_client", "markdown": "## CLIENT"},
    {"name": "statement", "markdown": "**{{label}}:** {{value}}"},
]
SECTIONED = Document(document_id="d1", text="# PROPOSAL\n\n## CLIENT\n**Name:** x\n")
STYLE = [{"name": "statement", "markdown": "m"}]


def output(**overrides) -> dict:
    return {"blocks": BLOCKS, "fields": FIELDS, "styles": STYLES, **overrides}


def block(id_, parent, type_="statement", text="x") -> dict:
    return {"id": id_, "parent_id": parent, "type": type_, "text": text}


def tree(*blocks) -> dict:
    return {"blocks": list(blocks), "fields": [], "styles": STYLE}


@pytest.mark.parametrize(
    "data,message",
    [
        (output(styles=STYLES[:2]), r"no style for element types: \['statement'\]"),
        (output(fields=FIELDS[:1]), r"spans without a field entry: \['region'\]"),
        (
            output(fields=FIELDS + [{"name": "region", "value": "Southern Region"}]),
            "'region' is given two different values",
        ),
        (output(styles=STYLES + [{"name": "title", "markdown": "## {{title}}"}]), "two different styles"),
        (
            output(blocks=[block("n1", "", "title", "Case {{ proposal_no }}")], styles=STYLES[:1], fields=[]),
            "proposal_no",
        ),  # inner spaces
        (tree(block("n1", ""), block("n1", "")), r"block ids used twice: \['n1'\]"),
        (tree(block("n1", "n9")), r"unknown blocks: \['n9'\]"),
        (tree(block("n1", "n1")), r"a parent must come before its children in the list: \['n1'\]"),
        (tree(block("n1", "n2"), block("n2", "")), r"a parent must come before its children in the list: \['n1'\]"),
        (
            output(styles=[], fields=[]),
            r"(?s)(?=.*no style for element types)(?=.*spans without a field entry)",
        ),  # all reported together
    ],
)
def test_invalid_analyzer_output_is_rejected(data, message):
    with pytest.raises(ValidationError, match=message):
        AnalyzerOutput.model_validate(data)


def test_the_prompt_defines_every_suggested_type_by_its_role_and_what_it_can_contain():
    """The splitter that cut documents into sections in code was needed because the type names had no definition."""
    prompt = DocumentAnalyzer().prompt
    for term in ("title", "metadata", "section", "subsection", "party_detail", "numbered_item", "bullet_point",
                 "statement", "signature_block", "divider"):  # fmt: skip
        assert f"\n- {term}: " in prompt, f"no definition for {term}"


def test_to_facts_builds_the_tree_from_the_parent_ids_and_turns_the_fields_into_a_map():
    out = AnalyzerOutput.model_validate(output(fields=FIELDS + [FIELDS[1]]))  # the same value twice is fine
    facts = out.to_facts("cfd_002")

    assert out.open_questions == []
    assert facts.document_id == "cfd_002"
    assert facts.fields == {"client_name": "Alder Robotics Ltd", "region": "Northern Region"}
    assert [n.text for n in facts.nodes] == ["# PROJECT PROPOSAL", "## CLIENT"]
    assert [c.text for c in facts.nodes[1].children] == ["**Name:** {{client_name}}", "**Region:** {{region}}"]


def test_deep_nesting_and_sibling_order_are_kept():
    blocks = [
        block("n1", ""),
        block("n2", "n1"),
        block("n3", "n2"),
        block("n4", "n2"),
        block("n5", "n1"),
        block("n6", ""),
    ]
    nodes = AnalyzerOutput.model_validate(tree(*blocks)).to_nodes()
    assert [len(n.children) for n in nodes] == [2, 0]
    assert [len(c.children) for c in nodes[0].children] == [2, 0]


def test_to_template_is_derived_from_the_models_tree_and_the_styles():
    template = AnalyzerOutput.model_validate(output()).to_template("project_proposals")
    assert template.category == "project_proposals"
    assert {e.name: (e.markdown, e.repeatable) for e in template.element_types} == {
        "title": ("# {{title}}", False),
        "party_client": ("## CLIENT", False),
        "statement": ("**{{label}}:** {{value}}", True),  # two statements under the same parent
    }


async def test_an_inconsistent_reply_is_not_accepted_and_a_corrected_one_is(db):
    bad = structured_reply("AnalyzerOutput", output(styles=[]))
    good = structured_reply("AnalyzerOutput", output())
    model = Scripted(script=[bad, good])

    result = await runner_with(db, model).run(DocumentAnalyzer, AnalyzerInput(document=SECTIONED, category="proposals"))

    assert result.status == RunStatus.SUCCESS
    assert model.calls == 2  # the first reply failed validation
    assert result.output.styles
