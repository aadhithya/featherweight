"""The Reconciliation Agent: one standard vocabulary of element types and field names across documents."""

from typing import ClassVar

from pydantic import Field, model_validator

import featherweight as ftw
from doclearn.models import DocumentTemplate, ElementType, FieldExamples, NameMapping

PROMPT = """\
You reconcile the element types and field names found in several documents of the same category into one \
standard vocabulary. The input is JSON with the category, `templates` (the element types of each document: name, \
markdown pattern, repeatable) and `fields` (every span name used in the documents, in how many documents it appears, \
and `contexts`). A context shows where a span is used: a `line` of a document with the span written as {{name}}, and \
the `value` that stands in its place in that document.

Look at the contexts to see what each name means. Names with the same meaning but different names are combined into \
one standard name. Do this for element types and for field names.

Return:
1. `type_mapping`: for each standard element type, the raw type names that mean the same.
2. `field_mapping`: for each standard field name, the names from `fields` that mean the same.
3. `element_types`: one entry for each standard type: its name, markdown pattern and whether it is repeatable.

Every raw type name and every name in `fields` appears in exactly one entry, and no other names are added. Standard names are descriptive snake_case.
"""


class ReconcilerInput(ftw.AgentInput):
    category: str = Field(description="The document category, provided by the user.")
    templates: list[DocumentTemplate] = Field(description="The template found in each analyzed document.")
    fields: list[FieldExamples] = Field(description="Every span name across the documents, with counts and contexts.")


class ReconcilerOutput(ftw.AgentOutput):
    type_mapping: list[NameMapping] = Field(default_factory=list, description="Standard element type to its raw names.")
    field_mapping: list[NameMapping] = Field(default_factory=list, description="Standard field name to its raw names.")
    element_types: list[ElementType] = Field(
        default_factory=list, description="The reconciled template: one per standard type."
    )

    @model_validator(mode="after")
    def _consistent(self) -> "ReconcilerOutput":
        """Rules the schema cannot express. A failure raises and the runner retries."""
        problems = []

        for label, mapping in (("type", self.type_mapping), ("field", self.field_mapping)):
            canonicals: set[str] = set()
            owner: dict[str, str] = {}
            for entry in mapping:
                if entry.canonical in canonicals:
                    problems.append(f"{label} '{entry.canonical}' is listed twice")
                canonicals.add(entry.canonical)
                for raw in entry.raw_names:
                    if owner.setdefault(raw, entry.canonical) != entry.canonical:
                        problems.append(
                            f"{label} name '{raw}' is mapped to both '{owner[raw]}' and '{entry.canonical}'"
                        )

        names = [e.name for e in self.element_types]
        if len(names) != len(set(names)):
            problems.append("element_types has duplicate names")
        canonical_types = {m.canonical for m in self.type_mapping}
        if set(names) != canonical_types:
            problems.append(
                f"element_types must match the standard types: missing {sorted(canonical_types - set(names))}, "
                f"unexpected {sorted(set(names) - canonical_types)}"
            )

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def type_map(self) -> dict[str, list[str]]:
        return {m.canonical: list(m.raw_names) for m in self.type_mapping}

    def field_map(self) -> dict[str, list[str]]:
        return {m.canonical: list(m.raw_names) for m in self.field_mapping}

    def to_template(self, category: str) -> DocumentTemplate:
        """The reconciled template, sorted by name (it has no ordering)."""
        return DocumentTemplate(category=category, element_types=sorted(self.element_types, key=lambda e: e.name))


class ReconciliationAgent(ftw.AgentBase):
    name: str = "reconciliation_agent"
    description: str = "Reconciles element type names and field names across documents into one standard vocabulary"
    version: str = "0.8.1"
    prompt: str = PROMPT
    model: ftw.ModelSettings = ftw.Gemini(
        model="gemini-3.8-flash", thinking="medium"
    )  # judging whether two names mean the same needs a stronger model
    input_model: ClassVar[type[ftw.AgentInput]] = ReconcilerInput
    output_model: ClassVar[type[ftw.AgentOutput]] = ReconcilerOutput
