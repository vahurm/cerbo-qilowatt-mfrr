"""Tests for the headless mFRR state machine (agent/mfrr_statemachine.py).

The DESS-off settle delay (threading.Timer) and the failsafe clock
(time.monotonic) are made deterministic by the ``timers`` and ``clock``
fixtures in conftest.py, so transitions are exercised with no real sleeping.
"""

from __future__ import annotations

import pytest

from mfrr_statemachine import MfrrController


def make_controller(actuator, **kw):
    params = dict(
        mfrr_sources=("fusebox", "kratt"),
        mqtt_lost_failsafe_s=300.0,
        max_duration_s=1800.0,
        dess_off_delay_s=2.0,
    )
    params.update(kw)
    return MfrrController(actuator, **params)


# --------------------------------------------------------------------------- #
# Entering / leaving an event
# --------------------------------------------------------------------------- #

def test_idle_non_mfrr_is_ignored(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="grid", mode="normal", power=0))
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


def test_frrdown_enters_active_then_applies_positive_setpoint(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="fusebox", mode="frrdown", power=3000))

    # DESS is dropped immediately; the signed setpoint waits for the settle timer.
    assert ctrl.state == "ACTIVE"
    assert actuator.names() == ["dess_off"]
    assert actuator.setpoints == []

    fired = timers.fire_pending()
    assert fired == 1
    assert actuator.setpoints == [3000]


def test_frrup_applies_negative_setpoint(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=5000))
    timers.fire_pending()
    assert actuator.setpoints == [-5000]


def test_active_setpoint_update_without_dess_toggle(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))
    timers.fire_pending()
    ctrl.on_workmode(make_command(power=6000))

    assert actuator.setpoints == [3000, 6000]
    # Only the initial entry toggles DESS off; no second dess_off/on in between.
    assert actuator.names().count("dess_off") == 1
    assert "dess_on" not in actuator.names()


def test_active_duplicate_setpoint_is_not_rewritten(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))
    timers.fire_pending()
    ctrl.on_workmode(make_command(power=3000))  # identical -> no-op
    assert actuator.setpoints == [3000]


def test_active_normal_command_reverts(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))
    timers.fire_pending()

    ctrl.on_workmode(make_command(source="grid", mode="normal", power=0))
    assert ctrl.state == "IDLE"
    # Revert releases the setpoint to 0, then restores DESS.
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


# --------------------------------------------------------------------------- #
# Failsafes (clock + tick)
# --------------------------------------------------------------------------- #

def test_failsafe_reverts_when_mqtt_lost_too_long(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))
    timers.fire_pending()

    ctrl.on_connected(False)
    clock.advance(200)
    ctrl.tick()
    assert ctrl.state == "ACTIVE"  # 200s < 300s threshold

    clock.advance(150)  # now 350s disconnected
    ctrl.tick()
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


def test_reconnect_before_threshold_keeps_event_active(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))
    timers.fire_pending()

    ctrl.on_connected(False)
    clock.advance(100)
    ctrl.on_connected(True)  # link restored
    clock.advance(500)
    ctrl.tick()
    assert ctrl.state == "ACTIVE"


def test_failsafe_reverts_after_max_duration(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))
    timers.fire_pending()

    clock.advance(1900)  # > 1800s, link still up
    ctrl.tick()
    assert ctrl.state == "IDLE"


def test_tick_in_idle_does_nothing(actuator, clock, timers):
    ctrl = make_controller(actuator)
    clock.advance(99999)
    ctrl.tick()
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


# --------------------------------------------------------------------------- #
# Shutdown + token race
# --------------------------------------------------------------------------- #

def test_shutdown_during_active_reverts_to_safe(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))
    timers.fire_pending()

    ctrl.shutdown()
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


def test_revert_before_delayed_setpoint_cancels_it(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=3000))  # timer pending, NOT fired yet
    assert actuator.setpoints == []

    # Revert (normal command) before the settle timer fires.
    ctrl.on_workmode(make_command(source="grid", mode="normal", power=0))
    assert ctrl.state == "IDLE"

    # The pending delayed setpoint was cancelled -> firing fires nothing.
    assert timers.fire_pending() == 0
    # The 3000 W setpoint must never have been written; only the revert's 0.
    assert 3000 not in actuator.setpoints
    assert actuator.setpoints == [0]


# --------------------------------------------------------------------------- #
# Command parsing robustness
# --------------------------------------------------------------------------- #

def test_power_none_is_treated_as_zero(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=None))
    timers.fire_pending()
    assert ctrl.state == "ACTIVE"
    assert actuator.setpoints == [0]


def test_power_as_string_is_parsed(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power="4000"))
    timers.fire_pending()
    assert actuator.setpoints == [4000]


def test_power_non_numeric_string_falls_back_to_zero(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power="oops"))
    timers.fire_pending()
    assert actuator.setpoints == [0]


def test_missing_source_is_non_mfrr(actuator, clock, timers):
    from conftest import Command

    ctrl = make_controller(actuator)
    ctrl.on_workmode(Command(Mode="frrdown", PowerLimit=3000))  # no _source
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


def test_source_and_mode_are_case_insensitive(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="FUSEBOX", mode="FRRUP", power=2000))
    timers.fire_pending()
    assert ctrl.state == "ACTIVE"
    assert actuator.setpoints == [-2000]
