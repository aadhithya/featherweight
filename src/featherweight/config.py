"""Every environment variable that tunes featherweight, in one list, read by one function (docs/specs/configuration.md).

A setting is **both ways** (the argument of the constructor wins, then the variable, then the default) or **environment only**
(the variable, then the default). Both are read when the object is built, and a value that is not a positive number raises
`ValueError` that names the variable. Every variable starts with `FTW_`.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Setting:
    """One variable: its name, its default, and whether it is a count (an integer) or a number of seconds. A default of `None`
    means that the setting has no value of its own: the object that reads it derives one (the heartbeat, from the lease)."""

    name: str
    default: float | None
    count: bool = False
    argument: str | None = None  # the keyword of the constructor that may override it ("both ways"), if there is one
    text: str = ""

    def read(self, argument: float | None = None) -> float | None:
        """The value: the argument if there is one, else the variable, else the default. `ValueError` names what was wrong."""
        if argument is not None:
            return self._checked(argument, self.argument or self.name)
        raw = os.environ.get(self.name, "").strip()
        if not raw:
            return self.default
        try:
            value: float = int(raw) if self.count else float(raw)
        except ValueError:
            raise ValueError(f"{self.name} must be {'an integer' if self.count else 'a number'}, got {raw!r}") from None
        return self._checked(value, self.name)

    def _checked(self, value: float, source: str) -> float:
        """`source` is what the error names: the variable, or the argument."""
        if isinstance(value, bool) or value != value or (self.count and int(value) != value):  # a bool, NaN, a fraction
            raise ValueError(f"{source} must be {'an integer' if self.count else 'a number'}, got {value!r}")
        if value <= 0 or (self.count and value < 1):
            raise ValueError(f"{source} must be {'at least 1' if self.count else 'greater than 0'}, got {value}")
        return int(value) if self.count else float(value)


def _count(name: str, default: int, text: str, argument: str | None = None) -> Setting:
    return Setting(name, default, count=True, argument=argument, text=text)


def _seconds(name: str, default: float | None, text: str) -> Setting:
    return Setting(name, default, text=text)


MAX_CONCURRENT_ATTEMPTS = _count(
    "FTW_MAX_CONCURRENT_ATTEMPTS", 10, "Model attempts running at once, per runner", argument="max_concurrent"
)
MAX_AGENT_DEPTH = _count("FTW_MAX_AGENT_DEPTH", 3, "Most agents in one chain of launches", argument="max_depth")
MAX_AGENT_LAUNCHES = _count(
    "FTW_MAX_AGENT_LAUNCHES", 10, "Most launches per attempt of one agent", argument="max_launches"
)
MAX_BACKGROUND_JOBS = _count(
    "FTW_MAX_BACKGROUND_JOBS",
    3,
    "Most children one attempt may have running in the background",
    argument="max_background_jobs",
)
WORKER_CONCURRENCY = _count("FTW_WORKER_CONCURRENCY", 10, "Jobs one worker runs at once", argument="concurrency")
OBSERVER_TIMEOUT = _seconds("FTW_OBSERVER_TIMEOUT", 1.0, "How long an observer hook may take")
WORKER_RECEIVE_TIMEOUT = _seconds("FTW_WORKER_RECEIVE_TIMEOUT", 1.0, "How long a worker waits for a message")
WORKER_CANCEL_POLL = _seconds("FTW_WORKER_CANCEL_POLL", 1.0, "How often a worker asks whether a job was cancelled")
JOB_LEASE = _seconds("FTW_JOB_LEASE", 60.0, "How long a worker holds a job without renewing it")
HEARTBEAT = _seconds("FTW_HEARTBEAT", None, "How often a worker renews its leases (a third of the job's lease)")
QUEUE_LEASE = _seconds("FTW_QUEUE_LEASE", 60.0, "How long a received message stays invisible to other receivers")
QUEUE_MAX_DELIVERIES = _count("FTW_QUEUE_MAX_DELIVERIES", 3, "Deliveries of a message before SqliteQueue marks it dead")

JOBS_LOOKUP_TIMEOUT = _seconds("FTW_JOBS_LOOKUP_TIMEOUT", 15.0, "How long jobs_lookup may take")

SETTINGS: tuple[Setting, ...] = (
    MAX_CONCURRENT_ATTEMPTS,
    MAX_AGENT_DEPTH,
    MAX_AGENT_LAUNCHES,
    MAX_BACKGROUND_JOBS,
    WORKER_CONCURRENCY,
    OBSERVER_TIMEOUT,
    WORKER_RECEIVE_TIMEOUT,
    WORKER_CANCEL_POLL,
    JOB_LEASE,
    HEARTBEAT,
    QUEUE_LEASE,
    QUEUE_MAX_DELIVERIES,
    JOBS_LOOKUP_TIMEOUT,
)
