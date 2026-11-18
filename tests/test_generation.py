"""Generation with a mocked LLM: the inputs, the checks that need no model, and the agent's own check tool."""

import json

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage, ToolMessage

from fakes import Scripted, runner_with, structured_reply, tool_reply
from doclearn.assembly import derive_facts_schema, required_inputs
from doclearn.generation import (
    MAX_CHECKS,
    GenerationError,
    check_generated,
    compile_tree,
    generate_document,
    render_structure,
    unused_inputs,
)
from doclearn.models import Condition, DocumentTemplate, ElementType, FieldInfo, LearnedNode, LearnedTree


def node(text, kind="fixed", *children, type_="statement", alternatives=None, conditions=None):
    return LearnedNode(type=type_, text=text, kind=kind, alternatives=alternatives or [], conditions=conditions or [], children=list(children))


def info(name, type_="text", calculated=False):
    return FieldInfo(name=name, type=type_, description=f"the {name}", calculated=calculated)


def cond(field, value="true"):
    return Condition(field=field, op="=", value=value)


def proposal_tree() -> LearnedTree:
    return LearnedTree(
        category="proposals",
        nodes=[
            node("# PROPOSAL", "fixed", type_="title"),
            node("## CLIENT", "fixed", node("**Name:** {{client_name}}", "slot"), type_="section"),
            node(
                "## PRICING", "fixed",
                node("- Total price: {{total}}.", "slot"),
                node("- Payment is due at the standard rate.", "slot", alternatives=["- Payment is due from {{due_date}}."]),
                node("- Training for two days.", "fixed", conditions=[cond("include_training")]),
                node("- The facts: {{summary}}", "generated"),
                type_="section",
            ),
        ],
        fields=[info("client_name"), info("total", "number", calculated=True), info("due_date", "date"),
                info("include_training", "boolean"), info("summary")],
    )


FACTS = {
    "client_name": "Alder Ltd", "total": "$100",
    "summary": "The vendor will deliver the platform in three phases over six weeks, starting with the data migration.",
}
FLAG_ON = {**FACTS, "include_training": "true"}

GOOD = """# PROPOSAL

## CLIENT
**Name:** Alder Ltd

## PRICING
- Total price: $100.
- Payment is due at the standard rate.
- The facts: the delivery was late by three weeks.
"""


# the schema: which inputs are required


def test_required_inputs_follow_the_wordings_conditions_and_generated_blocks_and_skip_calculated_fields():
    # `total` is in an unconditional slot but calculated; `due_date` is only in one of two wordings, so it is optional
    assert required_inputs(proposal_tree()) == {"client_name", "total"}
    names = [f.name for f in derive_facts_schema(proposal_tree()).fields]
    assert "total" not in names and names == ["client_name", "due_date", "include_training", "summary"]

    tree = LearnedTree(
        category="c",
        nodes=[node("## S", "fixed",
            node("- Interest from {{date}} at {{rate}}.", "slot", alternatives=["- Interest from {{date}}."]),  # date in both wordings
            node("- Optional {{extra}}", "slot", conditions=[cond("flag")]),  # a conditional block
            node("- Written: {{note}}", "generated"),  # generated
            type_="section")],
        fields=[info("date", "date"), info("rate"), info("extra"), info("flag", "boolean"), info("note")],
    )
    assert required_inputs(tree) == {"date"}
    schema = derive_facts_schema(tree)
    assert {f.name: f.required for f in schema.fields} == {"date": True, "rate": False, "extra": False, "flag": False, "note": False}


# the outline the agent follows


def test_the_outline_has_kinds_types_wordings_and_conditions_and_the_compiled_tree_drops_blocks_that_do_not_apply():
    tree = proposal_tree()
    outline = render_structure(tree).splitlines()
    assert outline[0] == 'fixed title "# PROPOSAL"'  # kind, element type, text
    assert outline[2] == '  slot statement "**Name:** {{client_name}}"'  # nested by indent
    assert any('slot statement "- Payment is due at the standard rate." | or "- Payment is due from {{due_date}}."' in line for line in outline)
    assert any('| only if include_training = "true"' in line for line in outline)  # the learned tree, not compiled

    cut = compile_tree(tree, FACTS)  # the flag is missing: it does not hold
    assert "- Training for two days." not in render_structure(cut)
    kept = compile_tree(tree, FLAG_ON)
    assert any(n.text == "- Training for two days." and n.conditions == [] for n in kept.nodes[2].children)  # conditions are gone
    assert "only if" not in render_structure(kept)
    assert any(n.conditions for n in tree.nodes[2].children)  # the input is not modified


# the checks that need no model


def checked(facts, markdown, tree=None):
    return check_generated(compile_tree(tree or proposal_tree(), facts), facts, markdown)


SHORT_LINES = LearnedTree(category="c", nodes=[node("---", "fixed"), node("Kind regards,", "fixed")], fields=[])
NUMBERED = LearnedTree(category="c", nodes=[node("2. **Optional Support:**", "fixed")], fields=[])
ADAPTING = LearnedTree(
    category="c",
    nodes=[node("- Introduce him as the project lead for this engagement.", "fixed"),
           node("**Name:** {{client_name}}", "slot"), node("**Date:** {{filed}}", "slot")],
    fields=[info("client_name"), info("filed", "date")],
)
ADAPTING_FACTS = {"client_name": "Nora Whitfield", "filed": "May 6, 2025"}
ADAPTED = "- Introduce her as the project lead for this engagement.\n**Name:** Nora Whitfield\n**Date:** May 6, 2025\n"
WITH_DATE = {**FACTS, "due_date": "March 20, 2024"}
DATED = GOOD.replace("- Payment is due at the standard rate.", "- Payment is due from March 20, 2024.")


@pytest.mark.parametrize(
    "facts,markdown,tree",
    [
        pytest.param(FACTS, GOOD, None, id="a correct document"),
        pytest.param(WITH_DATE, DATED, None, id="the wording the facts can fill"),
        pytest.param(FLAG_ON, GOOD + "- Training for two days.\n", None, id="a conditional block whose condition holds"),
        pytest.param({}, "---\nKind regards,\n", SHORT_LINES, id="short fixed lines on their own lines"),
        pytest.param({}, "3. **Optional Support:**\n", NUMBERED, id="leading list numbers are ignored"),
        pytest.param(ADAPTING_FACTS, ADAPTED, ADAPTING, id="small adaptations of a fixed line (gender)"),
    ],
)
def test_check_generated_accepts(facts, markdown, tree):
    assert checked(facts, markdown, tree) == []


BAD = GOOD.replace("Alder Ltd", "{{client_name}}").replace("## CLIENT", "## CUSTOMER DETAILS").replace("- Total price: $100.\n", "")
SHUFFLED = "## PRICING\n- Total price: $100.\n\n# PROPOSAL\n\n## CLIENT\n**Name:** Alder Ltd\n"
REWRITTEN = ADAPTED.replace("- Introduce her as the project lead for this engagement.", "- She will be in charge of the project.")


@pytest.mark.parametrize(
    "facts,markdown,tree,expected",  # each tuple of fragments must all be in one problem; ("",) means any problem
    [
        pytest.param(FACTS, BAD, None, [("unresolved", "client_name"), ("missing or too different", "## CLIENT"),
                                        ("missing or too different", "Total price")], id="placeholder, altered text, missing block"),
        pytest.param(FACTS, SHUFFLED, None, [("out of order",)], id="blocks out of order"),
        pytest.param(FACTS, DATED, None, [("",)], id="a wording the facts cannot fill"),
        pytest.param(FLAG_ON, GOOD, None, [("missing or too different", "Training")], id="a block whose condition holds is missing"),
        pytest.param({}, "Text with --- inside.\nKind regards,\n", SHORT_LINES, [("'---'",)], id="a short fixed line only inside other text"),
        pytest.param(ADAPTING_FACTS, ADAPTED.replace("May 6, 2025", "May 7, 2025"), ADAPTING,
                     [("a fact was changed", "May 6, 2025")], id="a fact is never changed"),
        pytest.param(ADAPTING_FACTS, REWRITTEN, ADAPTING, [("missing or too different",)], id="a rewritten fixed line"),
    ],
)
def test_check_generated_reports(facts, markdown, tree, expected):
    problems = checked(facts, markdown, tree)
    for fragments in expected:
        assert any(all(f in p for f in fragments) for p in problems), (fragments, problems)


def test_given_inputs_whose_value_is_nowhere_in_the_document_are_listed_but_flags_are_not():
    """A real run dropped an amount and a number of days that the learned tree had no slot for, without a word."""
    facts = {**FACTS, "due_date": "March 20, 2024", "include_training": "true"}
    assert unused_inputs(proposal_tree(), facts, GOOD) == ["due_date"]
    assert unused_inputs(proposal_tree(), {"client_name": "Alder  Ltd"}, GOOD) == []  # whitespace does not matter
    assert unused_inputs(proposal_tree(), {"due_date": "41"}, "Paid in 2041.") == ["due_date"]  # not "2041"
    assert unused_inputs(proposal_tree(), {"total": "$100"}, "Total price: $100.") == []


# the pipeline


@pytest_asyncio.fixture
async def learned(store):
    tree = proposal_tree()
    template = DocumentTemplate(category="proposals", element_types=[ElementType(name="statement", markdown="- {{text}}", repeatable=True)])
    learned_id = await store.save(category="proposals", template=template, tree=tree, facts_schema=derive_facts_schema(tree))
    return await store.get(learned_id)


def reply(notes=None):
    return structured_reply("GeneratorOutput", {"notes": notes or []})


def handing_in(markdown, notes=None):
    """The agent hands the document in through the tool, then returns its notes."""
    return [tool_reply("check_document", {"markdown": markdown}, "t1"), reply(notes)]


async def run(db, model, learned, facts=FACTS, **kwargs):
    return await generate_document(runner_with(db, model), learned, facts, **kwargs)


def sent(model, call=0) -> dict:
    return json.loads([m for m in model.seen[call] if isinstance(m, HumanMessage)][0].content)


def tool_outputs(model) -> list[str]:
    """What `check_document` returned to the agent, from the messages of its last call."""
    return [m.content for m in model.seen[-1] if isinstance(m, ToolMessage)]


async def test_a_correct_document_comes_back_with_the_optional_inputs_listed_and_the_tree_it_was_given(db, learned):
    model = Scripted(script=handing_in(GOOD, ["left out the replacement item"]))
    result = await run(db, model, learned, guidance="keep it short")

    assert result.markdown == GOOD and result.problems == [] and result.notes == ["left out the replacement item"]
    assert result.not_provided == ["due_date", "include_training"]
    payload = sent(model)
    assert payload["guidance"] == "keep it short"
    assert {f["name"]: f["value"] for f in payload["facts"]} == FACTS
    assert {f["name"]: f["required"] for f in payload["fields"]}["client_name"] is True
    assert [f["name"] for f in payload["fields"] if f["calculated"]] == ["total"]  # the agent is told what to compute
    assert "Replace the parts" not in payload["structure"] and "only if" not in payload["structure"]  # compiled for these facts


async def test_the_agent_checks_its_document_with_the_tool_and_fixes_what_it_reports(db, learned):
    broken = GOOD.replace("Alder Ltd", "{{client_name}}")
    model = Scripted(script=[
        tool_reply("check_document", {"markdown": broken}, "t1"),
        tool_reply("check_document", {"markdown": GOOD}, "t2"),
        reply(),
    ])
    result = await run(db, model, learned, facts={**FACTS, "due_date": "March 20, 2024"})

    assert result.markdown == GOOD and model.calls == 3  # one run: the checking happens inside the agent's own loop
    first, second = tool_outputs(model)
    assert first.startswith("Problems (1 of 3") and "client_name" in first
    assert second.startswith("OK")
    assert result.unused_inputs == ["due_date"]  # given, but the document has no place for it


async def test_after_three_failed_checks_the_tool_tells_the_agent_to_stop_and_report(db, learned):
    broken = "# PROPOSAL\n"
    model = Scripted(script=[tool_reply("check_document", {"markdown": broken}, f"t{i}") for i in range(MAX_CHECKS)]
                     + [reply(["could not fix: the client block is missing"])])
    result = await run(db, model, learned)

    outputs = tool_outputs(model)
    assert outputs[0].startswith("Problems (1 of 3") and outputs[2].startswith("FAILED after 3 checks")
    assert "List these problems in `notes`" in outputs[2]
    assert result.notes == ["could not fix: the client block is missing"]  # the agent's report reaches the caller
    assert result.markdown == broken and any("missing or too different" in p for p in result.problems)  # and the tool's own


async def test_a_run_that_ends_without_a_document_that_passed_the_check_fails(db, learned):
    never_checked = Scripted(script=[reply(["here is my document"])])
    with pytest.raises(GenerationError, match="without handing in a document"):
        await run(db, never_checked, learned)

    only_failed_once = Scripted(script=[tool_reply("check_document", {"markdown": "# PROPOSAL\n"}, "t1"), reply()])
    with pytest.raises(GenerationError, match="without handing in a document"):
        await run(db, only_failed_once, learned)


@pytest.mark.parametrize(
    "facts,match",
    [
        ({"summary": "x"}, r"Required inputs are missing: \['client_name'\]"),
        ({**FACTS, "plaintif_name": "typo"}, r"Unknown inputs.*\['plaintif_name'\]"),
        ({**FACTS, "client_name": "   "}, "client_name"),  # empty values count as not provided
    ],
)
async def test_bad_inputs_fail_before_any_model_call(db, learned, facts, match):
    with pytest.raises(GenerationError, match=match):
        await run(db, Scripted(script=handing_in(GOOD)), learned, facts=facts)
    assert await db.list_runs() == []
