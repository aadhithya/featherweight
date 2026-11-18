"""Schema rules for everything the models see: every field is described, and the output schemas are flat."""

import json

import pytest

from doclearn.alignment import SemanticAlignmentInput, SemanticAlignmentOutput
from doclearn.analyzer import AnalyzerInput, AnalyzerOutput
from doclearn.models import (
    Condition,
    Document,
    DocumentFacts,
    DocumentTemplate,
    ElementStyle,
    ElementType,
    FactBlock,
    FactNode,
    FieldExamples,
    FieldInfo,
    FieldValue,
    LearnedBlock,
    LearnedNode,
    NameMapping,
    UnitDocument,
)
from doclearn.reconciler import ReconcilerInput, ReconcilerOutput

OUTPUTS = [AnalyzerOutput, ReconcilerOutput, SemanticAlignmentOutput]
MODELS = OUTPUTS + [
    AnalyzerInput, ReconcilerInput, SemanticAlignmentInput,
    Document, FactNode, FactBlock, DocumentFacts, FieldValue, FieldExamples, NameMapping,
    ElementStyle, ElementType, DocumentTemplate, Condition, LearnedNode, LearnedBlock, FieldInfo, UnitDocument,
]  # fmt: skip


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_every_field_has_a_description_so_the_llm_knows_what_it_is(model):
    for name, field in model.model_fields.items():
        assert field.description and field.description.strip(), f"{model.__name__}.{name}"
    schema = model.model_json_schema()
    for part in [schema, *schema.get("$defs", {}).values()]:
        for name, prop in part.get("properties", {}).items():
            assert prop.get("description"), f"{part.get('title')}.{name} has no description in the JSON schema"


@pytest.mark.parametrize("model", OUTPUTS, ids=lambda m: m.__name__)
def test_output_schemas_are_flat_with_no_recursion_and_no_nullable_objects(model):
    """Gemini returned a nested `children` schema flat : the trees come as blocks with parent ids."""
    schema = model.model_json_schema()
    assert all("children" not in part.get("properties", {}) for part in schema.get("$defs", {}).values())
    assert "anyOf" not in json.dumps(schema)  # no `X | None` anywhere in what the model sees
