"""Structural checks on a learned tree, shared by the e2e test and its mocked unit tests.

A real run passed the e2e test with a tree that held every section twice (once inside the title unit, once as its
own unit) and a made-up `include_*` flag for each section: the old assertions (unit count, kinds, conformance) could
not see it. These checks can.
"""

import re

from doclearn.facts import heading_key, walk
from doclearn.models import Document, LearnedTree


def structural_problems(tree: LearnedTree, expected_sections: list[str]) -> list[str]:
    """What is wrong with the shape of the tree, given the sections every source document has.

    `expected_sections` are the headings (any markdown or numbering) that all the documents share; the tree must
    have exactly these units, in this order, each unit without a condition (every document has it), and no heading
    may appear twice anywhere in the tree.
    """
    problems = []

    units = [heading_key(node.text) for node in tree.nodes]
    wanted = [heading_key(h) for h in expected_sections]
    if units != wanted:
        problems.append(f"the units are {units}, expected {wanted}")

    for node in tree.nodes:
        if node.conditions:
            fields = ", ".join(f"{c.field} {c.op} {c.value}" for c in node.conditions)
            problems.append(f"unit '{node.text[:40]}' is conditional ({fields}) although every document has it")

    headings = [heading_key(n.text) for n in walk(tree.nodes) if n.text.lstrip().startswith("#")]
    repeated = sorted({h for h in headings if headings.count(h) > 1})
    if repeated:
        problems.append(f"headings that appear more than once in the tree: {repeated}")

    return problems


def shared_sections(documents: list[Document]) -> list[str]:
    """The level 1 and 2 markdown headings that every document has, in the order of the first document.

    For documents we have not read closely (the sample documents): the learned tree must have exactly these units.
    """
    heading = re.compile(r"^#{1,2}\s+(.+?)\s*$", re.MULTILINE)
    per_document = [{heading_key(m.group(0)) for m in heading.finditer(d.text)} for d in documents]
    common = set.intersection(*per_document) if per_document else set()
    return [m.group(0).strip() for m in heading.finditer(documents[0].text) if heading_key(m.group(0)) in common]
