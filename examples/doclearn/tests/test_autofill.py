"""The Form Filler with a mocked LLM: the checks that need no model, and the retry with the problems."""

import json

import pytest
import pytest_asyncio
from doclearn.assembly import derive_facts_schema
from doclearn.autofill import MAX_RETRIES, AutofillError, check_form, fill_form
from doclearn.models import DocumentTemplate, FieldInfo, FieldValue, InputField, LearnedNode, LearnedTree
from fakes import Scripted, runner_with, structured_reply


def field(name, type_="text", required=True):
    return InputField(name=name, type=type_, description=f"the {name}", required=required)


def values(**kw):
    return [FieldValue(name=n, value=v) for n, v in kw.items()]


FIELDS = [field("who"), field("amount", "number"), field("day", "date"), field("flag", "boolean", required=False)]


@pytest.mark.parametrize(
    "given, expected",
    [
        (values(who="Alder Ltd", amount="$5", day="March 4, 2024"), []),
        # empty values count as not given, and optional inputs may be left out
        (
            values(who="Alpine", amount=" ", day="March 4, 2024", flag=""),
            ["required fields without a value: ['amount']"],
        ),
        (
            values(who="A", amount="many", day="soon", flag="maybe", extra="x") + values(who="B"),
            [
                "values given more than once: ['who']",
                "not fields of the document: ['extra']",
                "amount: 'many' is not a valid number",
                "day: 'soon' is not a valid date",
                "flag: 'maybe' is not a valid boolean",
            ],
        ),
    ],
)
def test_check_form_reports_what_is_missing_unknown_duplicated_or_ill_typed(given, expected):
    assert sorted(check_form(FIELDS, given)) == sorted(expected)


@pytest_asyncio.fixture
async def learned(store):
    tree = LearnedTree(
        category="proposals",
        nodes=[LearnedNode(type="t", text="Quote for {{who}}: {{amount}}", kind="slot")],
        fields=[
            FieldInfo(name="who", type="text", description="the customer"),
            FieldInfo(name="amount", type="number", description="the sum"),
        ],
    )
    learned_id = await store.save(
        category="proposals",
        template=DocumentTemplate(category="proposals"),
        tree=tree,
        facts_schema=derive_facts_schema(tree),
    )
    return await store.get(learned_id)


def reply(facts, story="A story."):
    return structured_reply(
        "FormFillerOutput", {"facts": [{"name": n, "value": v} for n, v in facts.items()], "story": story}
    )


async def test_problems_go_back_to_the_agent_and_the_second_attempt_is_taken(db, learned):
    model = Scripted(script=[reply({"who": "Alpine"}), reply({"who": "Alder Ltd", "amount": "$5"}, " Long story. ")])
    lines: list[str] = []
    form = await fill_form(runner_with(db, model), learned, progress=lines.append)

    assert form.facts == {"who": "Alder Ltd", "amount": "$5"} and form.story == "Long story." and model.calls == 2
    first, second = (json.loads(model.seen[i][-1].content) for i in (0, 1))
    assert [f["name"] for f in first["fields"]] == ["who", "amount"] and "Quote for {{who}}" in first["structure"]
    assert second["problems"] == ["required fields without a value: ['amount']"] and "Alpine" in second["previous"]
    assert lines[0] == "Inventing a case" and lines[1].startswith("Inventing the case again (attempt 2)")


async def test_an_empty_story_is_a_problem_and_problems_after_the_retries_raise(db, learned):
    model = Scripted(script=[reply({"who": "A", "amount": "1"}, story=" ")] * (MAX_RETRIES + 1))
    with pytest.raises(AutofillError, match="still has problems.*story is empty"):
        await fill_form(runner_with(db, model), learned)
    assert model.calls == MAX_RETRIES + 1
