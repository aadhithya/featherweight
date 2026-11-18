"""Builders shared by the learning tests: small model objects (fact and learned nodes, facts, units, fields) and the
mocked replies of the whole learning pipeline (an Analyzer reply per document, the Reconciler reply, a Semantic
Alignment reply per unit), used by the pipeline and the CLI tests."""

import json

from fakes import flat_blocks, structured_reply
from doclearn.learning import AlignedUnit, ReconciledAnalysis
from doclearn.models import (
    Condition,
    DocumentFacts,
    DocumentTemplate,
    ElementType,
    FactNode,
    FieldInfo,
    LearnedNode,
)


def fnode(type_, text, *children):
    return FactNode(type=type_, text=text, children=list(children))


def lnode(type_, text, kind="fixed", *children, alternatives=None, conditions=None):
    return LearnedNode(
        type=type_, text=text, kind=kind, alternatives=alternatives or [], conditions=conditions or [], children=list(children)
    )


def facts(document_id, *nodes, **fields) -> DocumentFacts:
    return DocumentFacts(document_id=document_id, nodes=list(nodes), fields=fields)


def reconciled(*documents, types=("title", "section", "statement", "numbered_item")) -> ReconciledAnalysis:
    template = DocumentTemplate(
        category="proposals", element_types=[ElementType(name=n, markdown=f"md:{n}", repeatable=False) for n in types]
    )
    return ReconciledAnalysis(template=template, facts=list(documents), type_mapping={}, field_mapping={})


def info(name, type_="text", calculated=False, description="d"):
    return FieldInfo(name=name, type=type_, description=description, calculated=calculated)


def cond(field, op, value):
    return Condition(field=field, op=op, value=value)


def unit(position, node, *fields) -> AlignedUnit:
    return AlignedUnit(position=position, node=node, fields=list(fields))


def reply_node(type_, text, children=None):
    """A nested node of an Analyzer reply, written nested for readability (see `flat_blocks`)."""
    return {"type": type_, "text": text, "children": children or []}


STYLES = [{"name": n, "markdown": f"{n} pattern"} for n in ("title", "metadata", "section", "statement")]


def analyzer_reply(document_id: str):
    """One small proposal per document; `b` names the case number differently and has one more pricing line."""
    case = "proposal_num" if document_id == "b" else "proposal_no"
    pricing = [reply_node("statement", "- Total price: {{total_price}}.")]
    if document_id == "b":
        pricing.append(reply_node("statement", "- Add the optional training package."))
    return structured_reply(
        "AnalyzerOutput",
        {
            "blocks": flat_blocks(
                [
                    reply_node("title", "# PROPOSAL", [reply_node("metadata", "**Proposal No.:** {{%s}}" % case)]),
                    reply_node("section", "## CLIENT", [reply_node("statement", "**Name:** {{client_name}}")]),
                    reply_node("section", "## PRICING", pricing),
                ]
            ),
            "fields": [
                {"name": case, "value": f"PRP-{document_id}"},
                {"name": "client_name", "value": f"Client {document_id} Ltd"},
                {"name": "total_price", "value": f"${document_id}00,000"},
            ],
            "styles": STYLES,
        },
    )


def block(id_, parent, type_, text, kind="fixed", alternatives=None, conditions=None) -> dict:
    """A block of a Semantic Alignment reply (the agent only says whether it is generated)."""
    return {
        "id": id_, "parent_id": parent, "type": type_, "text": text, "generated": kind == "generated",
        "alternatives": alternatives or [], "conditions": conditions or [],
    }  # fmt: skip


def fld(name, type_="text", description="d", calculated=False) -> dict:
    return {"name": name, "type": type_, "description": description, "calculated": calculated}


def alignment_reply(position: int, pricing_text="## PRICING"):
    blocks, fields = {
        1: (
            [block("b1", "", "title", "# PROPOSAL"), block("b2", "b1", "metadata", "**Proposal No.:** {{proposal_no}}", "slot")],
            [fld("proposal_no", description="Case number")],
        ),
        2: (
            [block("b1", "", "section", "## CLIENT"), block("b2", "b1", "statement", "**Name:** {{client_name}}", "slot")],
            [fld("client_name", description="Client's name")],
        ),
        3: (
            [
                block("b1", "", "section", pricing_text),
                block("b2", "b1", "statement", "- Total price: {{total_price}}.", "slot"),
                block("b3", "b1", "statement", "- Add the optional training package.",
                      conditions=[{"field": "include_training", "op": "=", "value": "true"}]),
            ],
            [fld("total_price", "number", "Total price in USD"), fld("include_training", "boolean", "Include the training package?")],
        ),
    }[position]
    return structured_reply("SemanticAlignmentOutput", {"blocks": blocks, "fields": fields})


RECONCILER_REPLY = structured_reply(
    "ReconcilerOutput",
    {
        "type_mapping": [{"canonical": n, "raw_names": [n]} for n in ("title", "metadata", "section", "statement")],
        "field_mapping": [
            {"canonical": "proposal_no", "raw_names": ["proposal_no", "proposal_num"]},
            {"canonical": "client_name", "raw_names": ["client_name"]},
            {"canonical": "total_price", "raw_names": ["total_price"]},
        ],
        "element_types": [{"name": n, "markdown": f"{n} pattern", "repeatable": n in ("section", "statement")} for n in ("title", "metadata", "section", "statement")],
    },
)


def make_respond(fail_analyzing=(), fail_unit=None, pricing_text="## PRICING"):
    def respond(user_text, call_number):
        payload = json.loads(user_text)
        if "document" in payload:
            document_id = payload["document"]["document_id"]
            return RuntimeError(f"cannot analyze {document_id}") if document_id in fail_analyzing else analyzer_reply(document_id)
        if "templates" in payload:
            return RECONCILER_REPLY
        if payload["position"] == fail_unit:
            return RuntimeError("alignment down")
        return alignment_reply(payload["position"], pricing_text)

    return respond
