"""The runnable examples at least build: their agents are valid definitions (sub-agents, skills, schemas). No model calls."""

import importlib.util
from pathlib import Path

import pytest
from fakes import FakeSettings, Reactive, Scripted, structured_reply, use_model

from micro.jobs import run_job
from micro.model_settings import ModelSettings
from micro.models import AgentInput, AgentOutput, RunStatus
from micro.runner import AgentRunner

EXAMPLES = Path(__file__).parent.parent / "examples"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / name / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_launching_example_has_an_editor_that_can_launch_exactly_two_agents():
    editor = load("launching").Editor()
    assert [sub().name for sub in editor.subagents] == ["summariser", "translator"]
    assert editor.model.model == "gemini-3.5-flash-lite"
    assert "- summariser:" in editor.build_system_prompt() and "- translator:" in editor.build_system_prompt()


def test_the_skills_example_has_an_assistant_with_the_three_skills():
    assistant = load("skills").Assistant()
    assert sorted(skill.name for skill in assistant.skills) == ["email-reply", "meeting-notes", "release-notes"]
    assert assistant.model.model == "gemini-3.5-flash-lite"


@pytest.mark.parametrize("name", ["launching", "skills", "mcp"])
def test_the_examples_ask_for_a_request_or_use_their_samples(name):
    module = load(name)
    assert module.SAMPLES and all(isinstance(sample, str) and sample for sample in module.SAMPLES)


def test_the_mcp_example_reads_github_with_a_token_by_name_and_six_read_only_tools():
    agent = load("mcp").RepoReader()
    (server,) = agent.mcp_servers
    assert server.url == "https://api.githubcopilot.com/mcp/readonly" and server.bearer_from == "GITHUB_TOKEN"
    assert server.tools is not None and len(server.tools) == 6 and server.discover is False
    assert not any(word in tool for tool in server.tools for word in ("create", "update", "delete", "merge", "push"))
    assert agent.model.model == "gemini-3.5-flash-lite"


def test_the_doclearn_agents_build_and_are_valid_definitions():
    """The example's five agents are valid definitions: they build, and their settings, schemas and tools are consistent.
    The example has no tests of its own; `make pipeline` runs it for real."""
    from doclearn.alignment import SemanticAlignmentAgent
    from doclearn.analyzer import DocumentAnalyzer
    from doclearn.autofill import FormFillerAgent
    from doclearn.generation import GeneratorAgent
    from doclearn.reconciler import ReconciliationAgent

    classes = [DocumentAnalyzer, ReconciliationAgent, SemanticAlignmentAgent, GeneratorAgent, FormFillerAgent]
    agents = [cls() for cls in classes]
    assert len({agent.name for agent in agents}) == len(agents)
    for agent in agents:
        assert agent.model.model in agent.model.models
        assert issubclass(agent.input_model, AgentInput) and agent.input_model is not AgentInput  # its own input
        assert issubclass(agent.output_model, AgentOutput) and agent.output_model is not AgentOutput
        assert agent.build_system_prompt() and agent.system_prompt_hash
        assert len(set(agent.tool_names)) == len(agent.tool_names)


def test_the_resume_example_uses_the_smallest_claude_model_with_thinking_off():
    example = load("resume")
    for agent in (example.Summariser(), example.ReleaseNotes()):
        assert (agent.model.provider, agent.model.model) == ("anthropic", "claude-haiku-5-5")
        assert agent.model.label() == "off"


async def test_the_resume_example_crashes_after_the_summaries_and_the_resumed_run_reuses_them(db):
    """The example's own pipeline, on fake models: the first run raises after the summaries, and the second run, with
    `resume_from`, answers the summaries from the log and runs only the notes."""
    example = load("resume")

    class FakeSummariser(example.Summariser):
        model: ModelSettings = FakeSettings(key="summaries")

    class FakeWriter(example.ReleaseNotes):
        model: ModelSettings = FakeSettings(key="notes")

    summaries = Reactive(respond=lambda text, n: structured_reply("SummaryOutput", {"summary": "A change."}))
    notes = Scripted(script=[structured_reply("NotesOutput", {"notes": "The notes."})])
    use_model(summaries, "summaries")
    use_model(notes, "notes")
    runner = AgentRunner(db, retry_delay_s=0)
    agents = {"summariser": FakeSummariser, "writer": FakeWriter}
    commits = example.COMMITS

    first = await run_job(db, "t", lambda job_id: example.pipeline(runner, job_id, commits, crash=True, **agents))
    assert first.status == RunStatus.FAILED and "simulated crash" in (first.error or "")
    assert [r.agent_name for r in await db.list_runs(job_id=first.job_id)] == ["summariser"] * len(commits)

    calls_before = sum(summaries.counts.values())
    second = await run_job(
        db, "t", lambda job_id: example.pipeline(runner, job_id, commits, **agents), resume_from=first.job_id
    )
    assert second.status == RunStatus.SUCCESS and second.result == "The notes."
    assert sum(summaries.counts.values()) == calls_before  # the summaries were not asked for again
    assert notes.calls == 1
    rows = await db.list_runs(job_id=second.job_id)
    reused = [r for r in rows if r.reused_from_agent_run_id]
    assert [r.agent_name for r in reused] == ["summariser"] * len(commits)
    assert all((r.input_tokens, r.output_tokens) == (0, 0) for r in reused)
    assert [r.agent_name for r in rows if not r.reused_from_agent_run_id] == ["release_notes"]
