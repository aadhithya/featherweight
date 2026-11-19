"""The Document Analyzer agent: one document in, its facts (a typed content tree) and element styles out."""

from typing import ClassVar

from pydantic import Field, model_validator

import featherweight as ftw
from doclearn.facts import derive_template, find_spans
from doclearn.models import Document, DocumentFacts, DocumentTemplate, ElementStyle, FactBlock, FactNode, FieldValue

PROMPT = """\
You analyze one document and break it into typed content blocks. The input is JSON with the document \
(its id and markdown text) and its category.

Return three things:
1. `blocks`: the content of the whole document as a flat list of blocks, in document order.
2. `fields`: every fillable value you replaced with a {{span_name}}, with its exact value.
3. `styles`: one markdown pattern for each element type you used.

Rules for `blocks`:
- Cover the whole document, in order. Copy the text exactly as written, including its markdown syntax. Do not \
paraphrase, summarize, reorder or leave anything out.
- The element type is the structural role of the block in a document, not what it says. Use the standard \
terms below, whenever one fits; only if none does, name the new role in snake_case. The \
type follows the markup of the line: a line that starts with `- ` is a bullet_point and a line that \
starts with `1.` is a numbered_item, whatever it says. Use the same type for blocks that play the same role, so that \
it also fits the same kind of block in other documents of this category. Never put case-specific content in a type \
name.
- Show the hierarchy with ids. Give every block a unique `id` (n1, n2, n3, ...) and a `parent_id`: the id of the \
block it is nested under, or "" for a top-level block. A heading contains the blocks after it, up to the next \
heading of the same or a higher level, except the document title: it does not contain the sections after it, \
every section is a top-level block. The top level holds only the document title and the sections: every other \
block belongs to the title or to the section it falls under. A numbered item contains its bullets and sub-points. List items belong to \
the heading above them. List every parent before its children. A document with sections has nesting: do not leave \
every `parent_id` empty.
- Every line of the document is its own block, including consecutive short lines such as a label with its value \
(`**Colour:** ...` and `**Size:** ...` are two blocks). Never join several lines into one block. A block's `text` \
is its own line or sentence only; do not repeat the text of other blocks.

The element types:
- title: the name of the document as a whole. It can carry the metadata that identifies the document.
- metadata: a detail about the document itself rather than its substance (an identifier, a reference number, a date of issue).
- section: a major division of the document that groups related content under a heading. It can contain \
subsections, items and statements.
- subsection: a division inside a section. It can contain items and statements.
- party_detail: a fact that identifies a party or another person (a name, an address, a role, a representative).
- numbered_item: an enumerated element, such as a clause, a work item or a finding. It can contain bullet points.
- bullet_point: a listed element without a number, such as a detail, an exhibit or a sub-point.
- statement: running text that asserts, requests or declares something.
- signature_block: the closing by which the document is authenticated (a sign-off, a signatory, a date, a signature).
- divider: a visual separation with no content of its own. It is part of the title or section it closes.

Rules for fillable values:
- Replace each value that changes from case to case (names, addresses, dates, amounts, reference numbers, registration \
numbers, ids) with {{span_name}} in the block text, and list it in `fields` with its exact value as written.
- Keep standard wording as it is. Only case-specific values become spans.
- Span names are snake_case and role-based. When the line has a label (`**Place of Birth:** ...`, `- Travel costs: \
...`), build the name from the label text (place_of_birth, travel_costs): the label is the same in every document. \
A label that appears under several headings (a name, an address) gets the role of its heading in front of it. A span \
without a label is named after its role in the sentence. Never put case-specific content in a name.
- If the same value appears again, reuse its span name. If the same kind of value appears with a different value, \
give it a distinct name, e.g. contact_1_name and contact_2_name. Never give one name two different values.

Rules for `styles`:
- Give every element type you used exactly one pattern showing how it is formatted, with {{placeholders}} for the \
variable parts, e.g. "## {{heading}}", "- {{statement}}", "**{{label}}:** {{value}}".
"""


class AnalyzerInput(ftw.AgentInput):
    document: Document = Field(description="The document to analyze.")
    category: str = Field(description="The document category, provided by the user. For context only.")


class AnalyzerOutput(ftw.AgentOutput):
    blocks: list[FactBlock] = Field(
        default_factory=list, description="The document as a flat list of typed blocks with parent ids, in order."
    )
    fields: list[FieldValue] = Field(
        default_factory=list, description="Each {{span_name}} used in the blocks, with its value."
    )
    styles: list[ElementStyle] = Field(default_factory=list, description="One markdown pattern per element type used.")

    @model_validator(mode="after")
    def _consistent(self) -> "AnalyzerOutput":
        """Cross-field rules the schema cannot express. A failure raises and the runner retries."""
        problems = []

        ids = [b.id for b in self.blocks]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            problems.append(f"block ids used twice: {repeated}")
        position = {b.id: i for i, b in enumerate(self.blocks)}
        unknown = sorted({b.parent_id for b in self.blocks if b.parent_id and b.parent_id not in position})
        if unknown:
            problems.append(f"parent_id refers to unknown blocks: {unknown}")
        late = [b.id for i, b in enumerate(self.blocks) if b.parent_id in position and position[b.parent_id] >= i]
        if late:
            problems.append(f"a parent must come before its children in the list: {late}")

        styles: dict[str, str] = {}
        for style in self.styles:
            if styles.setdefault(style.name, style.markdown) != style.markdown:
                problems.append(f"element type '{style.name}' has two different styles")
        missing_styles = sorted({b.type for b in self.blocks} - styles.keys())
        if missing_styles:
            problems.append(f"no style for element types: {missing_styles}")

        values: dict[str, str] = {}
        for field in self.fields:
            if values.setdefault(field.name, field.value) != field.value:
                problems.append(f"field '{field.name}' is given two different values; use distinct span names")
        spans = {span for b in self.blocks for span in find_spans(b.text)}
        missing_fields = sorted(spans - values.keys())
        if missing_fields:
            problems.append(f"spans without a field entry: {missing_fields}")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def to_nodes(self) -> list[FactNode]:
        """Assemble the tree from the parent ids the model gave. Children keep the order of the list."""
        nodes = {b.id: FactNode(type=b.type, text=b.text) for b in self.blocks}
        top: list[FactNode] = []
        for block in self.blocks:
            (top if block.parent_id == "" else nodes[block.parent_id].children).append(nodes[block.id])
        return top

    def to_facts(self, document_id: str) -> DocumentFacts:
        """The facts of the document: the provided id, the tree of blocks, and the fields as a map."""
        return DocumentFacts(
            document_id=document_id,
            nodes=self.to_nodes(),
            fields={field.name: field.value for field in self.fields},
        )

    def to_template(self, category: str) -> DocumentTemplate:
        """The document's template, derived by code from the tree and the styles."""
        return derive_template(self.to_facts(""), {s.name: s.markdown for s in self.styles}, category)


class DocumentAnalyzer(ftw.AgentBase):
    name: str = "document_analyzer"
    description: str = "Breaks one document into typed content blocks, fillable fields and element styles"
    version: str = "0.5.1"
    prompt: str = PROMPT
    model: ftw.ModelSettings = ftw.Gemini(model="gemini-3.8-flash", thinking="low")
    input_model: ClassVar[type[ftw.AgentInput]] = AnalyzerInput
    output_model: ClassVar[type[ftw.AgentOutput]] = AnalyzerOutput
