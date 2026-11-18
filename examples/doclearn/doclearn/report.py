"""What the command line shows and saves about a learning run: the learned tree, the template, the inputs and how
well the source documents conform to the tree. Pure functions from the learned objects to text, no model, no database.

`describe_schema`, `describe_conformance` and `overview` take `full`: the terminal gets the short form, the files in
the run folder the full one.
"""

import json
from collections.abc import Mapping
from pathlib import Path

from doclearn.assembly import DocumentConformance
from doclearn.facts import walk
from doclearn.generation import render_structure
from doclearn.models import DocumentTemplate, FactsSchema, LearnedTree
from doclearn.pipeline import LearningResult
from doclearn.store import LearnedDocument

SHORT_MISMATCHES = 3  # per document on the terminal
TEXT_WIDTH = 60


def _clip(text: str, width: int = TEXT_WIDTH) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _kinds(nodes) -> str:
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node.kind] = counts.get(node.kind, 0) + 1
    return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))


def describe_tree(tree: LearnedTree) -> str:
    """The units of the tree with their size, and the conditional blocks."""
    nodes = list(walk(tree.nodes))
    conditional = [n for n in nodes if n.conditions]
    lines = [f"Tree: {len(tree.nodes)} units, {len(nodes)} blocks ({_kinds(nodes)}), {len(conditional)} conditional"]
    for position, root in enumerate(tree.nodes, 1):
        unit = list(walk([root]))
        lines.append(f"  {position}. {_clip(root.text)}  ({len(unit)} blocks: {_kinds(unit)})")
    for node in conditional:
        when = " and ".join(f"{c.field} {c.op} {c.value}" for c in node.conditions)
        lines.append(f"  only if {when}: {_clip(node.text)}")
    return "\n".join(lines)


def describe_template(template: DocumentTemplate) -> str:
    """The element types and their markdown patterns."""
    lines = [f"Template: {len(template.element_types)} element types"]
    for element in template.element_types:
        repeat = "  (repeatable)" if element.repeatable else ""
        lines.append(f"  {element.name}: {_clip(element.markdown.replace(chr(10), ' ⏎ '), 80)}{repeat}")
    return "\n".join(lines)


def describe_schema(schema: FactsSchema, *, full: bool = False) -> str:
    """The inputs, required first. Short form: the names and types; full form: each with its description."""
    lines = ["Inputs:"]
    for title, wanted in (("Required", True), ("Optional", False)):
        group = [f for f in schema.fields if f.required is wanted]
        if full:
            lines.append(f"  {title} ({len(group)}):")
            lines += [f"    {f.name} ({f.type}): {f.description}" for f in group]
        else:
            lines.append(f"  {title} ({len(group)}): " + ", ".join(f"{f.name} ({f.type})" for f in group))
    return "\n".join(lines)


def describe_conformance(result: LearningResult, *, full: bool = False) -> str:
    """How much of each source document the tree matches, the blocks it does not, and what the assembly warned about."""
    lines = ["Conformance of the source documents to the tree:"]
    for c in result.conformance:
        lines.append(f"  {c.document_id}: {c.score:.0%} ({c.matched}/{c.total} blocks)")
        shown = c.mismatches if full else c.mismatches[:SHORT_MISMATCHES]
        lines += [f"      - {_clip(m, 200 if full else 100)}" for m in shown]
        if len(shown) < len(c.mismatches):
            lines.append(f"      … {len(c.mismatches) - len(shown)} more in learned/overview.md")
    for failed in result.skipped:
        lines.append(f"  {failed.document_id}: skipped, it could not be analyzed ({_clip(failed.error, 100)})")
    if result.warnings:
        lines.append("Warnings:")
        lines += [f"  - {_clip(w, 200 if full else 100)}" for w in result.warnings]
    return "\n".join(lines)


def overview(learned: LearnedDocument, result: LearningResult, *, full: bool = False) -> str:
    """The whole report: tree, template, inputs, conformance."""
    parts = [
        describe_tree(learned.tree),
        describe_template(learned.template),
        describe_schema(learned.facts_schema, full=full),
        describe_conformance(result, full=full),
    ]
    return "\n\n".join(parts)


def min_conformance(conformance: list[DocumentConformance]) -> float:
    return min((c.score for c in conformance), default=1.0)


# the form file

GUIDANCE_KEY = "_guidance"


def form_file_content(schema: FactsSchema, facts: Mapping[str, str] | None = None, story: str = "") -> str:
    """The form as a JSON file to edit: required inputs first, each preceded by a `_` note with its type and
    description (keys starting with `_` are ignored when the file is read), and `_guidance` for the story."""
    facts = facts or {}
    form: dict[str, str] = {
        "_help": "Fill in the values of the new case. Optional inputs may stay empty. Keys starting with _ are notes, "
        f"except {GUIDANCE_KEY}: the story or extra instructions for the writer of the document.",
        GUIDANCE_KEY: story,
    }
    for field in sorted(schema.fields, key=lambda f: not f.required):  # stable: order of first use in each group
        form[f"_{field.name}"] = f"{'REQUIRED' if field.required else 'optional'}, {field.type}: {field.description}"
        form[field.name] = facts.get(field.name, "")
    return json.dumps(form, indent=2, ensure_ascii=False) + "\n"


# the run folder


def write_learned_files(folder: Path, learned: LearnedDocument, result: LearningResult) -> None:
    """Save the learned tree, template, inputs and conformance for review, as JSON and as text."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tree.json").write_text(learned.tree.model_dump_json(indent=2), encoding="utf-8")
    (folder / "template.json").write_text(learned.template.model_dump_json(indent=2), encoding="utf-8")
    (folder / "facts_schema.json").write_text(learned.facts_schema.model_dump_json(indent=2), encoding="utf-8")
    (folder / "conformance.json").write_text(
        json.dumps({"conformance": [c.model_dump() | {"score": c.score} for c in result.conformance],
                    "warnings": result.warnings, "skipped": [s.model_dump() for s in result.skipped]},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "outline.txt").write_text(render_structure(learned.tree) + "\n", encoding="utf-8")
    (folder / "overview.md").write_text(
        f"# Learned: {learned.category}\n\nlearned_id: {learned.learned_id}\n\n```\n{overview(learned, result, full=True)}\n```\n\n"
        f"## Outline (kind, text, other wordings, conditions)\n\n```\n{render_structure(learned.tree)}\n```\n",
        encoding="utf-8",
    )
