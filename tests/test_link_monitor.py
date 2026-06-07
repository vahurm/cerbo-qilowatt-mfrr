"""Tests for the QW link liveness watchdog in agent/qw_agent.py."""

from __future__ import annotations

import qw_agent


class _Clock:
    """Controllable replacement for time.monotonic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def _patch_clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(qw_agent.time, "monotonic", clock)
    return clock


def test_startup_counts_as_down(monkeypatch):
    clock = _patch_clock(monkeypatch)
    link = qw_agent.LinkMonitor()
    clock.advance(30)
    assert link.down_for() == 30.0


def test_connect_clears_down(monkeypatch):
    clock = _patch_clock(monkeypatch)
    link = qw_agent.LinkMonitor()
    clock.advance(30)
    link.set(True)
    clock.advance(100)
    assert link.down_for() == 0.0


def test_disconnect_starts_timer_and_accumulates(monkeypatch):
    clock = _patch_clock(monkeypatch)
    link = qw_agent.LinkMonitor()
    link.set(True)
    clock.advance(50)
    link.set(False)
    clock.advance(40)
    assert link.down_for() == 40.0


def test_repeated_disconnect_does_not_reset_timer(monkeypatch):
    clock = _patch_clock(monkeypatch)
    link = qw_agent.LinkMonitor()
    link.set(True)
    link.set(False)
    clock.advance(20)
    link.set(False)  # duplicate disconnect notification must not reset the clock
    clock.advance(20)
    assert link.down_for() == 40.0


def test_reconnect_then_drop_restarts_timer(monkeypatch):
    clock = _patch_clock(monkeypatch)
    link = qw_agent.LinkMonitor()
    link.set(False)
    clock.advance(100)
    link.set(True)
    clock.advance(100)
    link.set(False)
    clock.advance(10)
    assert link.down_for() == 10.0
