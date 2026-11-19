"""`import featherweight as ftw`: the public names are on the package, and loaded when they are first used."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import featherweight as ftw
from featherweight import _EXPORTS, _MODULES


@pytest.mark.parametrize("name", sorted(_EXPORTS))
def test_every_public_name_is_on_the_package(name):
    value = getattr(ftw, name)
    assert value is getattr(__import__(f"featherweight.{_EXPORTS[name]}", fromlist=[name]), name)
    assert name in ftw.__all__ and name in dir(ftw)


@pytest.mark.parametrize("name", _MODULES)
def test_every_module_is_reachable_from_the_package(name):
    assert getattr(ftw, name).__name__ == f"featherweight.{name}"


def test_the_adapters_are_reachable_as_attributes():
    assert ftw.agui.CONTENT_TYPE == ftw.adapters.agui.CONTENT_TYPE == "text/event-stream"
    assert ftw.vercel is ftw.adapters.vercel and issubclass(ftw.BadRequest, ValueError)


def test_an_unknown_name_is_an_attribute_error():
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        ftw.Nope  # noqa: B018


def test_importing_the_package_imports_no_framework_or_provider():
    code = (
        "import sys, featherweight as ftw; "
        "print(sorted(m for m in ('langchain', 'langgraph', 'langchain_anthropic', 'langchain_openai', 'aiosqlite') if m in sys.modules))"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env).stdout
    assert out.strip() == "[]"  # `import featherweight` is light; the names load when they are used


def test_a_whole_agent_can_be_written_with_the_alias_only():
    class Echo(ftw.AgentBase):
        name: str = "echo"
        description: str = "Echoes"
        version: str = "1"
        prompt: str = "Echo."
        model: ftw.ModelSettings = ftw.Claude(model="claude-haiku-5-5", effort="low", thinking="off")

    assert Echo().model.provider == "anthropic" and ftw.RunStatus.SUCCESS.value == "SUCCESS"
