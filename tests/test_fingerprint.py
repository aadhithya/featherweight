"""The agent hash: what it covers, and that equal declarations hash equal in any process."""

import subprocess
import sys
import textwrap
from pathlib import Path

from fakes import Echo, EchoInput, Scripted, runner_with, structured_reply
from langchain_core.tools import tool
from pydantic import model_validator

from micro.fingerprint import agent_hash
from micro.mcp import HttpMCPServer, StdioMCPServer
from micro.model_settings import Gemini
from micro.models import AgentBase
from micro.skills import Skill

ROOT = Path(__file__).parent.parent


@tool
def shout(text: str) -> str:
    """Shout the text."""
    return text.upper()


@tool
def whisper(text: str, quiet: bool = True) -> str:
    """Whisper the text."""
    return text.lower()


def echo(**kwargs) -> AgentBase:
    return Echo(**kwargs)


def make_chain(leaf_prompt: str) -> AgentBase:
    """Top launches Middle, which launches Leaf: the leaf is two levels down."""

    class Leaf(AgentBase):
        name: str = "leaf"
        description: str = "A leaf"
        version: str = "1"
        prompt: str = leaf_prompt

    class Middle(AgentBase):
        name: str = "middle"
        description: str = "A middle"
        version: str = "1"
        prompt: str = "Middle."
        subagents: list[type] = [Leaf]

    class Top(AgentBase):
        name: str = "top"
        description: str = "A top"
        version: str = "1"
        prompt: str = "Top."
        subagents: list[type] = [Middle]

    return Top()


def test_the_same_declaration_gives_the_same_hash():
    assert agent_hash(echo()) == agent_hash(echo())
    assert len(agent_hash(echo())) == 64


def test_the_hash_is_the_same_in_another_process():
    code = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, "tests")
        from fakes import Echo
        from micro.fingerprint import agent_hash
        print(agent_hash(Echo()))
        """
    )
    env = {"PYTHONPATH": str(ROOT / "src"), "PYTHONHASHSEED": "12345"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env, check=True)
    assert out.stdout.strip() == agent_hash(Echo())


def test_each_part_of_the_declaration_changes_the_hash():
    base = agent_hash(echo())
    changed = {
        "prompt": echo(prompt="Other."),
        "version": echo(version="0.2.0"),
        "description": echo(description="Other"),
        "name": echo(name="echo2"),
        "recursion_limit": echo(recursion_limit=10),
        "allow_user_questions": echo(allow_user_questions=True),
        "raise_conflicts": echo(raise_conflicts=True),
        "model": echo(model=Gemini(thinking="high")),
        "max_output_tokens": echo(model=Gemini(max_output_tokens=100)),
        "tool": echo(tools=[shout]),
        "skill": echo(skills=[Skill(name="notes", description="Notes.", instructions="One.")]),
        "mcp": echo(mcp_servers=[StdioMCPServer(name="fs", command="npx")]),
        "subagent": make_chain("Leaf."),
    }
    hashes = {key: agent_hash(agent) for key, agent in changed.items()}
    assert all(h != base for h in hashes.values())
    assert len(set(hashes.values())) == len(hashes)


def test_a_settings_class_with_the_same_fields_gives_another_hash():
    class OtherGemini(Gemini):
        pass

    assert agent_hash(echo(model=Gemini())) != agent_hash(echo(model=OtherGemini()))


def test_a_tools_description_and_schema_change_the_hash():
    described = shout.model_copy(update={"description": "Shout it."})
    assert agent_hash(echo(tools=[shout])) != agent_hash(echo(tools=[described]))
    other_schema = whisper.model_copy(update={"name": "shout", "description": shout.description})
    assert agent_hash(echo(tools=[shout])) != agent_hash(echo(tools=[other_schema]))


def test_a_skills_instructions_and_resources_change_the_hash():
    def hashed(**kw):
        return agent_hash(echo(skills=[Skill(name="notes", description="Notes.", **kw)]))

    assert hashed(instructions="One.") != hashed(instructions="Two.")
    assert hashed(resources={"a.md": "x"}) != hashed(resources={"a.md": "y"})


def test_an_mcp_server_setting_changes_the_hash():
    def hashed(server):
        return agent_hash(echo(mcp_servers=[server]))

    plain = StdioMCPServer(name="fs", command="npx")
    assert hashed(plain) != hashed(StdioMCPServer(name="fs", command="npx", args=("a",)))
    assert hashed(plain) != hashed(StdioMCPServer(name="fs", command="npx", discover=False))
    assert hashed(HttpMCPServer(name="h", url="http://x")) != hashed(HttpMCPServer(name="h", url="http://y"))


def test_a_subagent_two_levels_down_changes_the_hash():
    assert agent_hash(make_chain("One.")) == agent_hash(make_chain("One."))
    assert agent_hash(make_chain("One.")) != agent_hash(make_chain("Two."))


def test_the_order_of_dict_entries_does_not_change_the_hash():
    a = Skill(name="notes", description="Notes.", metadata={"a": "1", "b": "2"}, resources={"x": "1", "y": "2"})
    b = Skill(name="notes", description="Notes.", metadata={"b": "2", "a": "1"}, resources={"y": "2", "x": "1"})
    assert agent_hash(echo(skills=[a])) == agent_hash(echo(skills=[b]))
    h1 = HttpMCPServer(name="h", url="http://x", headers_from={"A": "X", "B": "Y"})
    h2 = HttpMCPServer(name="h", url="http://x", headers_from={"B": "Y", "A": "X"})
    assert agent_hash(echo(mcp_servers=[h1])) == agent_hash(echo(mcp_servers=[h2]))


def test_a_cycle_between_agents_does_not_loop_forever():
    # The model validator refuses a cycle (it would recurse while building), so these two skip that check.
    class A(AgentBase):
        name: str = "a"
        description: str = "A"
        version: str = "1"
        prompt: str = "A."

        @model_validator(mode="after")
        def _check_subagents(self):
            return self

    class B(A):
        name: str = "b"

    A.model_fields["subagents"].default_factory = lambda: [B]
    B.model_fields["subagents"].default_factory = lambda: [A]
    A.model_rebuild(force=True)
    B.model_rebuild(force=True)
    assert A().subagents == [B] and B().subagents == [A]
    assert len(agent_hash(A())) == 64


async def test_the_run_log_records_the_hash_and_leaves_the_prompt_hash_alone(db):
    model = Scripted(script=[structured_reply("EchoOutput", {"echoed": "hi"})])
    await runner_with(db, model).run(Echo, EchoInput(text="hi"))
    (row,) = await db.list_runs()
    assert row.agent_hash == agent_hash(Echo())
    assert row.system_prompt_hash == Echo().system_prompt_hash
    assert row.agent_hash != row.system_prompt_hash
