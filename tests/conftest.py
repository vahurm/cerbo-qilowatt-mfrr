"""Shared pytest fixtures and import-path wiring for the agent test suite.

The agent modules under ``agent/`` use flat, absolute imports
(``from actuators import ...``), so ``agent/`` must be on ``sys.path``. The
third-party ``qilowatt`` / ``paho`` packages come either from an installed
wheel (CI) or the vendored ``pylib/`` bundle (local dev / Cerbo); both are
added here so the tests run with no extra setup.
"""

from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_DIR = os.path.join(_REPO_ROOT, "agent")
_PYLIB_DIR = os.path.join(_REPO_ROOT, "pylib")
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")

for _path in (_AGENT_DIR, _PYLIB_DIR, _TOOLS_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

class FakeActuator:
    """Records the ordered sequence of actuator calls for assertions."""

    def __init__(self) -> None:
        self.calls: list = []

    def dess_off(self) -> None:
        self.calls.append(("dess_off",))

    def dess_on(self) -> None:
        self.calls.append(("dess_on",))

    def set_setpoint(self, watts) -> None:
        self.calls.append(("set_setpoint", int(watts)))

    # Convenience views ----------------------------------------------------- #
    @property
    def setpoints(self) -> list:
        return [c[1] for c in self.calls if c[0] == "set_setpoint"]

    def names(self) -> list:
        return [c[0] for c in self.calls]


class FakeReader:
    """Dict-backed stand-in for telemetry.base.DbusReader (no real dbus).

    ``values`` is keyed by ``(service, path)`` and ``services`` maps a bus-name
    prefix to the resolved service name returned by ``find_service``.
    """

    def __init__(self, values: dict | None = None, services: dict | None = None) -> None:
        self.values = dict(values or {})
        self.services = dict(services or {})
        self.available = True

    def find_service(self, prefix: str):
        return self.services.get(prefix)

    def get(self, service: str, path: str, default=None):
        return self.values.get((service, path), default)

    def get_float(self, service: str, path: str, default: float = 0.0) -> float:
        v = self.values.get((service, path), None)
        try:
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    def get_int(self, service: str, path: str, default: int = 0) -> int:
        return int(round(self.get_float(service, path, float(default))))


class Command:
    """Duck-typed WORKMODE command: the state machine only calls ``to_dict()``."""

    def __init__(self, **data) -> None:
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


# --------------------------------------------------------------------------- #
# Deterministic clock + timer control for the state machine
# --------------------------------------------------------------------------- #

class Clock:
    """Controllable replacement for ``time.monotonic`` used by the controller."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class _ManualTimer:
    """A threading.Timer drop-in that fires only when the test asks it to."""

    def __init__(self, registry, interval, function, args=None, kwargs=None) -> None:
        self._registry = registry
        self.interval = interval
        self.function = function
        self.args = tuple(args or ())
        self.kwargs = dict(kwargs or {})
        self.cancelled = False
        self.daemon = False

    def start(self) -> None:
        self._registry.timers.append(self)

    def cancel(self) -> None:
        self.cancelled = True


class TimerRegistry:
    """Collects scheduled timers so a test can fire pending (non-cancelled) ones."""

    def __init__(self) -> None:
        self.timers: list = []

    def fire_pending(self) -> int:
        pending = [t for t in self.timers if not t.cancelled]
        self.timers = []
        for t in pending:
            t.function(*t.args, **t.kwargs)
        return len(pending)


@pytest.fixture
def actuator() -> FakeActuator:
    return FakeActuator()


@pytest.fixture
def clock(monkeypatch) -> Clock:
    import mfrr_statemachine

    c = Clock()
    monkeypatch.setattr(mfrr_statemachine.time, "monotonic", c)
    return c


@pytest.fixture
def timers(monkeypatch) -> TimerRegistry:
    import mfrr_statemachine

    registry = TimerRegistry()

    def factory(interval, function, args=None, kwargs=None):
        return _ManualTimer(registry, interval, function, args=args, kwargs=kwargs)

    monkeypatch.setattr(mfrr_statemachine.threading, "Timer", factory)
    return registry


@pytest.fixture
def make_command():
    def _make(source="fusebox", mode="frrdown", power=3000, **extra):
        data = {"_source": source, "Mode": mode, "PowerLimit": power}
        data.update(extra)
        return Command(**data)

    return _make
