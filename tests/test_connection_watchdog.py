"""Tests for ConnectionWatchdog — the exit-for-restart decision logic.

The watchdog exists because qilowatt-py's connection callback is not a reliable
liveness signal (it reports "connected" after a failed re-subscribe, and can
stay "connected" on a silently deaf socket). See qw_agent.ConnectionWatchdog.
"""

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


def _watchdog(**kwargs) -> qw_agent.ConnectionWatchdog:
    defaults = dict(link_restart_s=600.0, subscribe_grace_s=120.0, idle_refresh_s=3600.0)
    defaults.update(kwargs)
    return qw_agent.ConnectionWatchdog(**defaults)


def _healthy(wd) -> str:
    """Evaluate with a fully healthy connected+subscribed+IDLE link."""
    return wd.check(transport_connected=True, subscribed=True, link_down_s=0.0, state="IDLE")


# --------------------------------------------------------------------------- #
# 1) link-down failsafe
# --------------------------------------------------------------------------- #

def test_healthy_link_never_restarts(monkeypatch):
    _patch_clock(monkeypatch)
    wd = _watchdog()
    assert _healthy(wd) is None


def test_link_down_beyond_threshold_restarts(monkeypatch):
    _patch_clock(monkeypatch)
    wd = _watchdog(link_restart_s=600.0)
    assert wd.check(transport_connected=False, subscribed=False, link_down_s=599.0, state="IDLE") is None
    reason = wd.check(transport_connected=False, subscribed=False, link_down_s=600.0, state="IDLE")
    assert reason is not None and "link down" in reason


def test_link_restart_disabled(monkeypatch):
    _patch_clock(monkeypatch)
    wd = _watchdog(link_restart_s=0.0)
    assert wd.check(transport_connected=False, subscribed=False, link_down_s=99999.0, state="IDLE") is None


# --------------------------------------------------------------------------- #
# 2) connected-but-not-subscribed zombie (the documented qilowatt-py trap)
# --------------------------------------------------------------------------- #

def test_connected_not_subscribed_triggers_after_grace(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(subscribe_grace_s=120.0)
    # First observation only starts the timer.
    assert wd.check(transport_connected=True, subscribed=False, link_down_s=0.0, state="IDLE") is None
    clock.advance(119)
    assert wd.check(transport_connected=True, subscribed=False, link_down_s=0.0, state="IDLE") is None
    clock.advance(1)
    reason = wd.check(transport_connected=True, subscribed=False, link_down_s=0.0, state="IDLE")
    assert reason is not None and "subscription dead" in reason


def test_subscription_recovers_before_grace_resets_timer(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(subscribe_grace_s=120.0)
    wd.check(transport_connected=True, subscribed=False, link_down_s=0.0, state="IDLE")
    clock.advance(60)
    # Subscription came back — the bad-since timer must reset.
    assert wd.check(transport_connected=True, subscribed=True, link_down_s=0.0, state="IDLE") is None
    clock.advance(60)
    # Goes bad again; only 60s in, must not trip yet.
    assert wd.check(transport_connected=True, subscribed=False, link_down_s=0.0, state="IDLE") is None


def test_transport_down_does_not_count_as_subscription_failure(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(subscribe_grace_s=120.0)
    # During a normal reconnect the transport is down and unsubscribed; that is
    # the link failsafe's job, not the subscribe watchdog's.
    for _ in range(5):
        clock.advance(60)
        assert wd.check(transport_connected=False, subscribed=False, link_down_s=10.0, state="IDLE") is None


# --------------------------------------------------------------------------- #
# 3) command-silence refresh backstop (keyed on time since last WORKMODE)
# --------------------------------------------------------------------------- #

def test_refresh_after_command_silence(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(idle_refresh_s=3600.0)
    # No command ever received: silence is measured from startup.
    clock.advance(3599)
    assert _healthy(wd) is None
    clock.advance(1)
    reason = _healthy(wd)
    assert reason is not None and "no WORKMODE command received" in reason


def test_refresh_skipped_while_active(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(idle_refresh_s=3600.0)
    clock.advance(7200)
    # An mFRR event is in progress — never interrupt it.
    assert wd.check(transport_connected=True, subscribed=True, link_down_s=0.0, state="ACTIVE") is None


def test_received_command_resets_silence_timer(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(idle_refresh_s=3600.0)
    clock.advance(3000)
    wd.note_command()  # a command arrived before the threshold
    clock.advance(3599)
    assert _healthy(wd) is None  # only 3599s since the last command
    clock.advance(1)
    reason = _healthy(wd)
    assert reason is not None and "no WORKMODE command received" in reason


def test_periodic_commands_prevent_refresh(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(idle_refresh_s=3600.0)
    # Hourly-heartbeat site: a command every 30 min (< threshold) -> never refreshes.
    for _ in range(20):
        clock.advance(1800)
        wd.note_command()
        assert _healthy(wd) is None


def test_refresh_disabled(monkeypatch):
    clock = _patch_clock(monkeypatch)
    wd = _watchdog(idle_refresh_s=0.0)
    clock.advance(100000)
    assert _healthy(wd) is None
