"""Configuration: every `FTW_` variable, which settings are both ways, and that the spec, the guide and the code say the same."""

import logging
import re
from pathlib import Path

import pytest
from fakes import Scripted, runner_with, settings

from featherweight import config
from featherweight.queue import SqliteQueue
from featherweight.testing import MemoryQueue
from featherweight.worker import JobWorker

ROOT = Path(__file__).parent.parent
BOTH_WAYS = [s for s in config.SETTINGS if s.argument]
ENV_ONLY = [s for s in config.SETTINGS if not s.argument]


def test_every_variable_starts_with_the_prefix_and_is_listed_once():
    names = [s.name for s in config.SETTINGS]
    assert all(n.startswith("FTW_") for n in names) and len(set(names)) == len(names)


@pytest.mark.parametrize("setting", config.SETTINGS, ids=lambda s: s.name)
def test_a_setting_has_its_default_comes_from_the_environment_and_blank_means_the_default(monkeypatch, setting):
    monkeypatch.delenv(setting.name, raising=False)  # the test suite shortens the polls of a worker
    assert setting.read() == setting.default
    monkeypatch.setenv(setting.name, "7")
    assert setting.read() == 7
    monkeypatch.setenv(setting.name, "  ")
    assert setting.read() == setting.default


@pytest.mark.parametrize("setting", config.SETTINGS, ids=lambda s: s.name)
@pytest.mark.parametrize("value", ["0", "-3", "many", "nan"])
def test_a_value_that_is_not_a_positive_number_names_the_variable(monkeypatch, setting, value):
    monkeypatch.setenv(setting.name, value)
    with pytest.raises(ValueError, match=setting.name):
        setting.read()


@pytest.mark.parametrize("setting", [s for s in config.SETTINGS if s.count], ids=lambda s: s.name)
def test_a_count_is_a_whole_number(monkeypatch, setting):
    monkeypatch.setenv(setting.name, "1.5")
    with pytest.raises(ValueError, match="an integer"):
        setting.read()


@pytest.mark.parametrize("setting", BOTH_WAYS, ids=lambda s: s.name)
def test_a_setting_that_can_be_passed_in_code_has_the_argument_win_over_the_variable(monkeypatch, setting):
    monkeypatch.setenv(setting.name, "4")
    assert setting.read(2) == 2
    with pytest.raises(ValueError, match=setting.argument):
        setting.read(0)  # the error names the argument, not the variable


def test_the_settings_of_an_object_are_read_when_it_is_built_and_not_after(monkeypatch, db):
    monkeypatch.setenv("FTW_WORKER_CONCURRENCY", "3")
    runner = runner_with(db, Scripted(script=[]))
    worker = JobWorker(db, MemoryQueue(), runner, [])
    monkeypatch.setenv("FTW_WORKER_CONCURRENCY", "9")
    assert worker.concurrency == 3
    assert JobWorker(db, MemoryQueue(), runner, []).concurrency == 9
    assert JobWorker(db, MemoryQueue(), runner, [], concurrency=1).concurrency == 1  # the argument wins


def test_the_environment_only_settings_reach_the_worker_and_the_queue(db):
    runner = runner_with(db, Scripted(script=[]))
    with settings(
        job_lease=30,
        heartbeat=4,
        worker_receive_timeout=0.2,
        worker_cancel_poll=0.3,
        queue_lease=8,
        queue_max_deliveries=2,
    ):
        worker = JobWorker(db, MemoryQueue(), runner, [])
        queue = SqliteQueue(db)
    assert (worker.lease, worker.heartbeat, worker.receive_timeout, worker.cancel_poll) == (30, 4, 0.2, 0.3)
    assert (queue.lease, queue.max_deliveries) == (8, 2)
    default = JobWorker(db, MemoryQueue(), runner, [])
    assert default.heartbeat == default.lease / 3  # no FTW_HEARTBEAT: a third of the job's lease


@pytest.mark.parametrize("kwargs", [{"lease": 5}, {"heartbeat": 5}, {"receive_timeout": 1}, {"cancel_poll": 1}])
def test_a_setting_that_is_environment_only_has_no_argument(db, kwargs):
    with pytest.raises(TypeError):
        JobWorker(db, MemoryQueue(), runner_with(db, Scripted(script=[])), [], **kwargs)
    with pytest.raises(TypeError):
        SqliteQueue(db, lease=5)  # ty: ignore[unknown-argument]
    with pytest.raises(TypeError):
        runner_with(db, Scripted(script=[]), observer_timeout=1)


def test_the_old_names_do_nothing(monkeypatch, db):
    for old in ("MAX_CONCURRENT_ATTEMPTS", "MAX_AGENT_DEPTH", "MAX_AGENT_LAUNCHES"):
        monkeypatch.setenv(old, "2")
    runner = runner_with(db, Scripted(script=[]))
    assert (runner.max_concurrent, runner.max_depth, runner.max_launches) == (10, 3, 10)


def test_the_runner_and_the_worker_log_the_values_they_were_built_with(db, caplog):
    with caplog.at_level(logging.INFO):
        with settings(max_concurrent_attempts=4, job_lease=30):
            runner = runner_with(db, Scripted(script=[]))
            JobWorker(db, MemoryQueue(), runner, [])
    assert "runner_configured max_concurrent=4" in caplog.text
    assert "worker_configured" in caplog.text and "lease=30.0" in caplog.text


def table_names(text: str) -> set[str]:
    return set(re.findall(r"^\| `(FTW_[A-Z_]+)`", text, re.M))


def test_the_spec_the_guide_and_the_code_list_the_same_variables():
    in_code = {s.name for s in config.SETTINGS} | {"FTW_DB_PATH", "FTW_LOG_LEVEL"}
    spec = table_names((ROOT / "docs/specs/configuration.md").read_text())
    guide = table_names((ROOT / "docs/agents.md").read_text())
    assert in_code == spec == guide


def test_the_spec_says_which_settings_can_be_passed_in_code():
    spec = (ROOT / "docs/specs/configuration.md").read_text()
    for setting in config.SETTINGS:
        row = next(line for line in spec.splitlines() if line.startswith(f"| `{setting.name}`"))
        assert (f"({setting.argument}=)" in row) if setting.argument else ("| none |" in row), setting.name
