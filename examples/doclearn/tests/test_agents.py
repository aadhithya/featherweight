import pytest
from doclearn.alignment import SemanticAlignmentAgent, SemanticAlignmentInput, SemanticAlignmentOutput
from doclearn.analyzer import AnalyzerInput, AnalyzerOutput, DocumentAnalyzer
from doclearn.reconciler import ReconcilerInput, ReconcilerOutput, ReconciliationAgent


@pytest.mark.parametrize(
    "agent_class,input_model,output_model",
    [
        (DocumentAnalyzer, AnalyzerInput, AnalyzerOutput),
        (ReconciliationAgent, ReconcilerInput, ReconcilerOutput),
        (SemanticAlignmentAgent, SemanticAlignmentInput, SemanticAlignmentOutput),
    ],
)
def test_agent_definition(agent_class, input_model, output_model):
    agent = agent_class()
    assert agent.input_model is input_model and agent.output_model is output_model
    assert agent.allow_user_questions is False and agent.raise_conflicts is False and agent.tools == []
