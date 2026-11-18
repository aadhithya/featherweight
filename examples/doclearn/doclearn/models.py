"""Data models of the learning pipeline."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(_Strict):
    """An input document: the caller reads the file and passes its id and markdown text."""

    document_id: str = Field(description="Id of the document, provided by the caller.")
    text: str = Field(description="Full markdown text of the document.")


class FactNode(_Strict):
    """One content block of a document. The hierarchy lives here, not in the template."""

    type: str = Field(
        description="Generic element type of this block, e.g. title, section, party_client, bullet_point."
    )
    text: str = Field(description="The block's own text as written, with {{span_name}} in place of fillable values.")
    children: list["FactNode"] = Field(default_factory=list, description="Nested blocks, in document order.")


class FactBlock(_Strict):
    """A block of a document as the Analyzer returns it: flat, with ids, so the schema has no recursion.

    A nested `children` schema came back flat from Gemini (depth 1, no children in a real run).
    The tree is assembled from the parent ids the model gives; the hierarchy is the model's.
    """

    id: str = Field(description="Unique id of this block within the document, e.g. n1, n2.")
    parent_id: str = Field(description="Id of the block this one is nested under; empty for a top-level block.")
    type: str = Field(
        description="Generic element type of this block, e.g. title, section, party_client, bullet_point."
    )
    text: str = Field(description="The block's own text as written, with {{span_name}} in place of fillable values.")


class DocumentFacts(_Strict):
    """A document's content classified into element types, as a tree."""

    document_id: str = Field(description="Id of the document, as provided.")
    nodes: list[FactNode] = Field(default_factory=list, description="Top-level blocks, in document order.")
    fields: dict[str, str] = Field(default_factory=dict, description="Span name to the value found in this document.")


class FieldValue(_Strict):
    name: str = Field(description="Span name, as used in {{span_name}} in a node's text.")
    value: str = Field(description="The value found in the document, exactly as written.")


class FieldContext(_Strict):
    line: str = Field(description="A line of a document where the name is used, with {{name}} in place.")
    value: str = Field(description="The value the name had in that document.")


class FieldExamples(_Strict):
    name: str = Field(description="A span name as used in the documents.")
    documents: int = Field(description="In how many documents this span name appears.")
    contexts: list[FieldContext] = Field(
        default_factory=list,
        description="Up to three places where this name is used: the line and the value it had there.",
    )


class NameMapping(_Strict):
    canonical: str = Field(description="The standard name.")
    raw_names: list[str] = Field(description="Every raw name, as found in the documents, that means the same thing.")


class ElementStyle(_Strict):
    name: str = Field(description="Element type name, as used in a node's `type`.")
    markdown: str = Field(description="Markdown pattern for this type with {{placeholders}}, e.g. '## {{heading}}'.")


class ElementType(ElementStyle):
    repeatable: bool = Field(description="True if several nodes of this type share a parent.")


class DocumentTemplate(_Strict):
    """The unique element types of a document. No hierarchy and no ordering: the order of a
    document comes from its facts tree. `element_types` is sorted by name only so that it is
    deterministic."""

    category: str = Field(description="Document category, provided by the user.")
    element_types: list[ElementType] = Field(default_factory=list, description="Unique element types, sorted by name.")


class Condition(_Strict):
    field: str = Field(description="Name of the field the condition tests, e.g. salary or jurisdiction.")
    op: Literal["=", "!=", ">", ">=", "<", "<="] = Field(description="Comparison; = and != also match text.")
    value: str = Field(
        description="Value to compare with, e.g. 5000 or Bayern. Numbers and dates compare by the field type."
    )


class LearnedNode(_Strict):
    """A node of the learned tree: like a FactNode, plus kind, alternatives and a condition."""

    type: str = Field(description="Canonical element type of this block.")
    text: str = Field(description="Canonical wording of the block, with {{span_name}} for case-specific values.")
    kind: Literal["fixed", "slot", "generated"] = Field(
        description="fixed: same everywhere, no spans. slot: same sentence, case-specific values as spans. "
        "generated: varies with the case and is written for each case."
    )
    alternatives: list[str] = Field(
        default_factory=list, description="Other wordings of this block found in other documents."
    )
    conditions: list[Condition] = Field(
        default_factory=list, description="Include this block, and its children, only if all of these hold."
    )
    children: list["LearnedNode"] = Field(default_factory=list, description="Nested blocks, in order.")


class LearnedBlock(_Strict):
    """A block of the learned tree as the agent returns it: flat, with ids, so the schema has no recursion.

    A nested schema (recursive children, nullable nested objects) came back silently truncated from Gemini.
    """

    id: str = Field(description="Unique id of this block within the unit, e.g. b1, b2.")
    parent_id: str = Field(description="Id of the block this one is nested under; empty for the section's root block.")
    type: str = Field(description="Canonical element type of this block.")
    text: str = Field(description="Canonical wording of the block, with {{span_name}} for case-specific values.")
    generated: bool = Field(
        description="True if the content of the block varies with the case itself (the facts of the case, "
        "a narrative, a justification) and is written for each case. False for standard wording, also when it has {{spans}} or "
        "other phrasings."
    )
    alternatives: list[str] = Field(
        default_factory=list, description="Other wordings of this block found in other documents."
    )
    conditions: list[Condition] = Field(
        default_factory=list, description="Include this block, and its children, only if all of these hold."
    )


class FieldInfo(_Strict):
    name: str = Field(description="Field name, as used in {{spans}} or in a condition.")
    type: Literal["text", "number", "date", "boolean"] = Field(description="What kind of value the field holds.")
    description: str = Field(description="One short line saying what to enter; helps the user and the model.")
    calculated: bool = Field(
        default=False, description="True if the value is computed from other fields, e.g. a total."
    )


class LearnedTree(_Strict):
    """The normalised document tree: what was learned from the documents. Unit k is at position k."""

    category: str = Field(description="Document category, provided by the user.")
    nodes: list[LearnedNode] = Field(description="The units, in position order.")
    fields: list[FieldInfo] = Field(description="Every described field, including calculated ones.")


class InputField(FieldInfo):
    """A field of the facts schema: an input of generation. Derived by code from the learned tree, not by the SA."""

    required: bool = Field(
        default=True,
        description="False if the document can be written without it: it is only in some wordings of its blocks, "
        "in a conditional block, in a generated block, or is a condition flag.",
    )


class FactsSchema(_Strict):
    """The inputs to collect from a user, agent, caller or API to generate a document. No calculated fields."""

    category: str = Field(description="Document category, provided by the user.")
    fields: list[InputField] = Field(description="The fields to fill in, in order of first use in the tree.")


class UnitDocument(_Strict):
    document_id: str = Field(description="Id of the document.")
    node: FactNode = Field(
        description="This document's section for the unit: its heading node, with its whole subtree."
    )
    fields: list[FieldValue] = Field(description="The spans used in that subtree, with their values in this document.")


FactNode.model_rebuild()
LearnedNode.model_rebuild()
