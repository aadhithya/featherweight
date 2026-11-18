"""Code-only helpers on document facts: derive a template, and apply the Reconciliation Agent's mappings."""

import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from typing import TypeVar

from doclearn.models import DocumentFacts, DocumentTemplate, ElementType, FactNode, LearnedNode

SPAN_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


class MappingError(ValueError):
    """A mapping cannot be applied without losing or contradicting information."""


def find_spans(text: str) -> list[str]:
    """The span names used in a text, in order, with surrounding whitespace removed."""
    return [m.group(1) for m in SPAN_RE.finditer(text)]


Node = TypeVar("Node", FactNode, LearnedNode)  # a tree of facts, or a learned tree


def walk(nodes: Sequence[Node]) -> Iterator[Node]:
    """Every node in the tree, depth-first, parents before their children."""
    for node in nodes:
        yield node
        yield from walk(node.children)


_MARKS = re.compile(r"[#*_`>]+")
_ROMAN = r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_NUMBERING = re.compile(rf"^(?:\d+|{_ROMAN})[.)]\s+")


def heading_key(text: str) -> str:
    """A heading as it is compared across documents: spans, markdown marks, case, spacing and numbering removed.

    "### IV. RELIEF SOUGHT" and "## 4. Relief  sought" both give "relief sought".
    """
    text = _MARKS.sub(" ", SPAN_RE.sub("", text))
    return _NUMBERING.sub("", " ".join(text.split()).casefold())


def derive_template(facts: DocumentFacts, styles: Mapping[str, str], category: str) -> DocumentTemplate:
    """A document's template is the unique element types of its facts.

    A type is `repeatable` if more than one node of that type has the same parent (the top level
    counts as a parent). `styles` maps each element type to its markdown, as emitted by the
    Analyzer; a node type without a style is an error, and extra styles are ignored.
    """
    used = {node.type for node in walk(facts.nodes)}
    missing = sorted(used - styles.keys())
    if missing:
        raise ValueError(f"No markdown style for element types: {missing}")

    repeatable: set[str] = set()

    def visit(siblings: Sequence[FactNode]) -> None:
        counts = Counter(node.type for node in siblings)
        repeatable.update(t for t, n in counts.items() if n > 1)
        for node in siblings:
            visit(node.children)

    visit(facts.nodes)
    return DocumentTemplate(
        category=category,
        element_types=[ElementType(name=t, markdown=styles[t], repeatable=t in repeatable) for t in sorted(used)],
    )


def _invert(mapping: Mapping[str, Sequence[str]], kind: str) -> dict[str, str]:
    """`{canonical: [raw, ...]}` to `{raw: canonical}`. A raw name under two canonicals is an error."""
    raw_to_canonical: dict[str, str] = {}
    for canonical, raws in mapping.items():
        for raw in raws:
            if raw_to_canonical.get(raw, canonical) != canonical:
                raise MappingError(f"{kind} '{raw}' is mapped to both '{raw_to_canonical[raw]}' and '{canonical}'")
            raw_to_canonical[raw] = canonical
    return raw_to_canonical


def split_conflicting_merges(
    field_mapping: Mapping[str, Sequence[str]], documents: Sequence[DocumentFacts]
) -> tuple[dict[str, list[str]], list[str]]:
    """Keep names apart that cannot be one field: a field has one value per document.

    If two names of a merge group are both in one document with different values (`delay_days` = "16 days" and
    `delay_duration` = "3 weeks" in the same document), they are different facts, whatever their names suggest. The group
    is split into sub-groups without such a pair; the sub-group with the canonical name keeps it (or the first one, if
    the canonical name is a new name), the others are named after their first raw name. Returns the new mapping and one
    note for each split. Values are compared with whitespace normalised.
    """

    def value(facts: DocumentFacts, name: str) -> str | None:
        return " ".join(facts.fields[name].split()) if name in facts.fields else None

    def clash(first: str, second: str) -> bool:
        return any(
            (a := value(f, first)) is not None and (b := value(f, second)) is not None and a != b for f in documents
        )

    result: dict[str, list[str]] = {}
    notes: list[str] = []
    for canonical, raws in field_mapping.items():
        groups: list[list[str]] = []
        for raw in raws:
            home = next((g for g in groups if not any(clash(raw, other) for other in g)), None)
            if home is None:
                groups.append([raw])
            else:
                home.append(raw)
        if len(groups) == 1:
            result[canonical] = list(raws)
            continue
        keeper = next((g for g in groups if canonical in g), groups[0])
        for group in groups:
            name = canonical if group is keeper else group[0]
            while name in result or (group is not keeper and name in field_mapping):
                name += "_2"
            result[name] = group
        notes.append(f"{canonical}: {[g for g in groups]}")
    return result, notes


def apply_mappings(
    facts: DocumentFacts,
    type_mapping: Mapping[str, Sequence[str]],
    field_mapping: Mapping[str, Sequence[str]],
) -> DocumentFacts:
    """Rename node types, `{{span}}` names in node text, and `fields` keys to their canonical names.

    Both mappings are `{canonical: [raw_names]}`. Names that appear in no mapping are left as they
    are. Spans in the text come out normalised as `{{name}}`. Returns a new object. Raises
    `MappingError` if two raw names map to one canonical field name in the same document with
    different values.
    """
    types = _invert(type_mapping, "Element type")
    fields = _invert(field_mapping, "Field")

    def rename_span(match: re.Match[str]) -> str:
        name = match.group(1)
        return "{{" + fields.get(name, name) + "}}"

    def rename_node(node: FactNode) -> FactNode:
        return FactNode(
            type=types.get(node.type, node.type),
            text=SPAN_RE.sub(rename_span, node.text),
            children=[rename_node(child) for child in node.children],
        )

    renamed_fields: dict[str, str] = {}
    for name, value in facts.fields.items():
        canonical = fields.get(name, name)
        if renamed_fields.get(canonical, value) != value:
            raise MappingError(
                f"Document '{facts.document_id}': field '{canonical}' would get two different values "
                f"({renamed_fields[canonical]!r} and {value!r})"
            )
        renamed_fields[canonical] = value

    return DocumentFacts(
        document_id=facts.document_id,
        nodes=[rename_node(node) for node in facts.nodes],
        fields=renamed_fields,
    )
