"""The prompts must not be fitted to the test documents: no example is taken from the fixtures."""

import re
from pathlib import Path

import pytest
from doclearn.alignment import SemanticAlignmentAgent
from doclearn.analyzer import DocumentAnalyzer
from doclearn.autofill import FormFillerAgent
from doclearn.generation import GeneratorAgent
from doclearn.reconciler import ReconciliationAgent

FIXTURES = Path(__file__).parent / "fixtures"
AGENTS = [DocumentAnalyzer, ReconciliationAgent, SemanticAlignmentAgent, GeneratorAgent, FormFillerAgent]


def fixture_headings() -> set[str]:
    """Every markdown heading line of the fixture documents, without the # marks."""
    headings = set()
    for path in FIXTURES.rglob("*.md"):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if match:
                headings.add(match.group(1))
    return headings


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.__name__)
def test_no_prompt_quotes_a_heading_of_the_fixture_documents(agent):
    prompt = agent().prompt
    quoted = sorted(h for h in fixture_headings() if h in prompt)
    assert quoted == [], f"{agent.__name__} quotes fixture headings: {quoted}"


def fixture_labels() -> set[str]:
    """Every `**Label:**` of the fixture and example documents, without the marks."""
    labels = set()
    for folder in (FIXTURES, FIXTURES.parent.parent / "samples"):
        for path in folder.rglob("*.md"):
            labels |= set(re.findall(r"\*\*([^*\n]+?):\*\*", path.read_text(encoding="utf-8")))
    return labels


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.__name__)
def test_no_prompt_quotes_a_label_of_the_fixture_or_example_documents(agent):
    prompt = agent().prompt
    quoted = sorted(label for label in fixture_labels() if f"**{label}:**" in prompt)
    assert quoted == [], f"{agent.__name__} quotes labels of the documents: {quoted}"


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.__name__)
def test_no_prompt_contains_a_json_example_of_its_own_output(agent):
    """A worked example copied from the data is easy for a model to copy back; the schema already gives the shape."""
    assert "Example of the shape" not in agent().prompt
