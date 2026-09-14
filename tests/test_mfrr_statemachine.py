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

def test_power_none_does_not_open_an_event(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power=None))
    timers.fire_pending()
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


def test_power_as_string_is_parsed(actuator, clock, timers, make_command):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power="4000"))
    timers.fire_pending()
    assert actuator.setpoints == [4000]


def test_power_non_numeric_string_does_not_open_an_event(
    actuator, clock, timers, make_command
):
    """Unparseable power must not drop DESS and lower the SOC floor either."""
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(power="oops"))
    timers.fire_pending()
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


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


# --------------------------------------------------------------------------- #
# Mode gate: a trusted source may also speak non-FRR dialects
#
# Live evidence (site A + site B, 2026-07): `_source='qilowatt'` sends both
# frrup/frrdown balancing dispatch and `buy` optimiser trades on the same
# topic. Without the mode gate a `buy` is signed `+abs(PowerLimit)` and lands
# in the actuators as a full-power grid import with DESS disabled.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["buy", "sell", "normal", "", "surprise"])
def test_non_frr_mode_from_mfrr_source_is_ignored(
    actuator, clock, timers, make_command, mode
):
    ctrl = make_controller(actuator, mfrr_sources=("fusebox", "kratt", "qilowatt"))
    ctrl.on_workmode(make_command(source="qilowatt", mode=mode, power=10000))
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


def test_buy_during_active_event_reverts_when_trades_are_disabled(
    actuator, clock, timers, make_command
):
    """With QW_TRADE_MODES empty a buy is still a foreign Mode: it ends the
    event rather than becoming a full-power import (pre-2026-09 behaviour)."""
    ctrl = make_controller(
        actuator, mfrr_sources=("fusebox", "kratt", "qilowatt"), trade_modes=()
    )
    ctrl.on_workmode(make_command(source="qilowatt", mode="frrup", power=10000))
    timers.fire_pending()
    assert actuator.setpoints == [-10000]

    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=10000))
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]
    assert 10000 not in actuator.setpoints


@pytest.mark.parametrize(
    "mode,expected", [("frrup", -10000), ("frrdown", 11000)]
)
def test_qilowatt_source_frr_is_actuated_once_enabled(
    actuator, clock, timers, make_command, mode, expected
):
    ctrl = make_controller(actuator, mfrr_sources=("fusebox", "kratt", "qilowatt"))
    ctrl.on_workmode(make_command(source="qilowatt", mode=mode, power=abs(expected)))
    timers.fire_pending()
    assert ctrl.state == "ACTIVE"
    assert actuator.setpoints == [expected]


def test_qilowatt_source_stays_ignored_when_not_configured(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)  # default sources: fusebox, kratt
    ctrl.on_workmode(make_command(source="qilowatt", mode="frrup", power=10000))
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


# --------------------------------------------------------------------------- #
# Q trades: `_source='qilowatt'` Mode buy/sell actuated as a `trade` event
#
# Live evidence (site A, 2026-07-14..09-14): `qilowatt`/`buy` (PowerLimit
# 20-27 kW, BatterySoc 100) arrives 5-30 min after a `kratt`/`frrup` stand-down
# and is followed 5-30 min later by the next frrup — the vendor refills the
# battery between activations. Dropped, the next activation runs from the floor.
# --------------------------------------------------------------------------- #

TRADE_SOURCES = ("fusebox", "kratt", "qilowatt")


def make_trade_controller(actuator, **kw):
    params = dict(
        mfrr_sources=TRADE_SOURCES,
        trade_sources=("qilowatt",),
        trade_modes=("buy", "sell"),
        max_trade_s=5400.0,
    )
    params.update(kw)
    return make_controller(actuator, **params)


def test_buy_opens_a_trade_with_positive_setpoint_and_no_floor_change(
    actuator, clock, timers, make_command
):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000, BatterySoc=100))

    assert ctrl.state == "ACTIVE"
    assert ctrl.kind == "trade"
    # DESS goes off but the SOC floor is left alone: a buy charges upward.
    assert actuator.calls == [("dess_off", "--no-floor")]
    timers.fire_pending()
    assert actuator.setpoints == [27000]


def test_sell_opens_a_trade_with_negative_setpoint(actuator, clock, timers, make_command):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="sell", power=8000, BatterySoc=20))
    timers.fire_pending()
    assert ctrl.kind == "trade"
    assert actuator.setpoints == [-8000]


def test_trade_is_capped_to_the_connection_limits(actuator, clock, timers, make_command):
    """The setpoint script would REJECT 27 kW against a 15 kW cap and leave the
    previous setpoint in place; the agent caps so something is delivered."""
    ctrl = make_trade_controller(actuator, max_import_w=28000, max_export_w=15000)
    ctrl.on_workmode(make_command(source="qilowatt", mode="sell", power=27000))
    timers.fire_pending()
    assert actuator.setpoints == [-15000]

    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    assert actuator.setpoints[-1] == 27000  # under the import cap: untouched


def test_frr_is_capped_too_when_limits_are_configured(actuator, clock, timers, make_command):
    ctrl = make_trade_controller(actuator, max_import_w=28000, max_export_w=15000)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=16000))
    timers.fire_pending()
    assert actuator.setpoints == [-15000]


def test_no_limits_means_no_cap(actuator, clock, timers, make_command):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=99000))
    timers.fire_pending()
    assert actuator.setpoints == [99000]


@pytest.mark.parametrize("mode", ["buy", "sell"])
def test_zero_power_trade_is_a_stand_down(actuator, clock, timers, make_command, mode):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode=mode, power=0))
    assert ctrl.state == "IDLE"
    assert actuator.calls == []

    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=20000))
    timers.fire_pending()
    assert ctrl.state == "ACTIVE"
    ctrl.on_workmode(make_command(source="qilowatt", mode=mode, power=0))
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


def test_trade_from_a_non_trade_source_is_dropped(actuator, clock, timers, make_command):
    """`kratt`/`buy` never happens live; if it did it must not become an import."""
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="kratt", mode="buy", power=20000))
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


@pytest.mark.parametrize("mode", ["savebattery", "limitexport", "normal", "pvsell", "nobattery"])
def test_only_buy_and_sell_can_ever_be_trade_modes(actuator, clock, timers, make_command, mode):
    """A misconfigured QW_TRADE_MODES must not widen the gate past buy/sell."""
    ctrl = make_trade_controller(actuator, trade_modes=("buy", "sell", mode))
    ctrl.on_workmode(make_command(source="qilowatt", mode=mode, power=20000))
    assert ctrl.state == "IDLE"
    assert actuator.calls == []


def test_trade_setpoint_update_does_not_toggle_dess(actuator, clock, timers, make_command):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=20000))
    timers.fire_pending()
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    assert actuator.setpoints == [20000, 27000]
    assert actuator.names().count("dess_off") == 1
    assert "dess_on" not in actuator.names()


def test_update_before_settle_timer_is_written_once_by_the_timer(
    actuator, clock, timers, make_command
):
    """A second command inside the DESS-off settle window must not write the
    setpoint early; the pending timer writes the latest value once."""
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=20000))
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    assert actuator.setpoints == []
    timers.fire_pending()
    assert actuator.setpoints == [27000]


# --- FRR <-> trade transitions ---------------------------------------------- #

def test_frr_dispatch_during_trade_switches_kind_without_dess_cycle(
    actuator, clock, timers, make_command
):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000, BatterySoc=100))
    timers.fire_pending()
    assert actuator.calls == [("dess_off", "--no-floor"), ("set_setpoint", 27000)]

    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=15000))
    assert ctrl.state == "ACTIVE"
    assert ctrl.kind == "frr"
    # No dess_on in between; the floor is lowered now that dispatch needs it.
    assert actuator.calls[2:] == [("dess_off",), ("set_setpoint", -15000)]
    assert "dess_on" not in actuator.names()


def test_trade_during_frr_switches_kind_without_dess_cycle(
    actuator, clock, timers, make_command
):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=15000))
    timers.fire_pending()

    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000, BatterySoc=100))
    assert ctrl.kind == "trade"
    assert actuator.calls[2:] == [("set_setpoint", 27000)]
    assert "dess_on" not in actuator.names()

    # A later frrup stand-down ends the trade too: one WorkMode channel.
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=0))
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


def test_kind_switch_restarts_the_duration_clock(actuator, clock, timers, make_command):
    ctrl = make_trade_controller(actuator, max_duration_s=1800.0, max_trade_s=5400.0)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    timers.fire_pending()
    clock.advance(1700)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=15000))
    clock.advance(1000)  # 2700 s since the trade began, 1000 s since dispatch
    ctrl.tick()
    assert ctrl.state == "ACTIVE"
    clock.advance(900)   # 1900 s since dispatch > 1800 s
    ctrl.tick()
    assert ctrl.state == "IDLE"


def test_non_event_command_ends_a_trade(actuator, clock, timers, make_command, caplog):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    timers.fire_pending()
    with caplog.at_level("INFO"):
        ctrl.on_workmode(make_command(source="notimer", mode="normal", power=0))
    assert ctrl.state == "IDLE"
    end = [r.getMessage() for r in caplog.records if "TRADE END" in r.getMessage()]
    assert len(end) == 1 and "notimer/normal" in end[0]


# --- SOC target ---------------------------------------------------------------- #

class _Soc:
    def __init__(self, value):
        self.value = value
        self.reads = 0

    def __call__(self):
        self.reads += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_buy_ends_when_live_soc_reaches_target(actuator, clock, timers, make_command, caplog):
    soc = _Soc(60.0)
    ctrl = make_trade_controller(actuator, soc_reader=soc)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000, BatterySoc=100))
    timers.fire_pending()

    ctrl.tick()
    assert ctrl.state == "ACTIVE"
    soc.value = 99.0
    ctrl.tick()
    assert ctrl.state == "ACTIVE"
    soc.value = 100.0
    with caplog.at_level("INFO"):
        ctrl.tick()
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]
    end = [r.getMessage() for r in caplog.records if "TRADE END" in r.getMessage()]
    assert len(end) == 1 and "SOC 100% >= 100%" in end[0]


def test_sell_ends_when_live_soc_drops_to_target(actuator, clock, timers, make_command):
    soc = _Soc(50.0)
    ctrl = make_trade_controller(actuator, soc_reader=soc)
    ctrl.on_workmode(make_command(source="qilowatt", mode="sell", power=8000, BatterySoc=30))
    timers.fire_pending()
    ctrl.tick()
    assert ctrl.state == "ACTIVE"
    soc.value = 29.0
    ctrl.tick()
    assert ctrl.state == "IDLE"


def test_trade_without_battery_soc_field_runs_until_command_or_cap(
    actuator, clock, timers, make_command
):
    soc = _Soc(100.0)
    ctrl = make_trade_controller(actuator, soc_reader=soc)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))  # no BatterySoc
    timers.fire_pending()
    ctrl.tick()
    assert ctrl.state == "ACTIVE"
    assert soc.reads == 0


@pytest.mark.parametrize("failure", [None, RuntimeError("dbus down")])
def test_soc_read_failure_never_ends_a_trade(actuator, clock, timers, make_command, failure):
    """Fail-open toward continuing: the next command or the cap ends it."""
    ctrl = make_trade_controller(actuator, soc_reader=_Soc(failure))
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000, BatterySoc=100))
    timers.fire_pending()
    ctrl.tick()
    assert ctrl.state == "ACTIVE"


def test_soc_target_is_not_applied_to_frr(actuator, clock, timers, make_command):
    """frrup carries BatterySoc too (the floor, e.g. 6) — it is not a target."""
    soc = _Soc(50.0)
    ctrl = make_trade_controller(actuator, soc_reader=soc)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=15000, BatterySoc=6))
    timers.fire_pending()
    ctrl.tick()
    assert ctrl.state == "ACTIVE"
    assert soc.reads == 0


def test_kind_switch_to_frr_drops_the_trade_soc_target(actuator, clock, timers, make_command):
    soc = _Soc(100.0)
    ctrl = make_trade_controller(actuator, soc_reader=soc)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000, BatterySoc=100))
    timers.fire_pending()
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=15000, BatterySoc=6))
    ctrl.tick()
    assert ctrl.state == "ACTIVE"  # SOC 100 >= 100 must not end the dispatch


# --- Failsafes on trades ---------------------------------------------------- #

def test_trade_has_its_own_duration_cap(actuator, clock, timers, make_command, caplog):
    ctrl = make_trade_controller(actuator, max_duration_s=1800.0, max_trade_s=5400.0)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    timers.fire_pending()
    clock.advance(1900)   # past the FRR cap, under the trade cap
    ctrl.tick()
    assert ctrl.state == "ACTIVE"
    clock.advance(3600)   # 5500 s > 5400 s
    with caplog.at_level("INFO"):
        ctrl.tick()
    assert ctrl.state == "IDLE"
    end = [r.getMessage() for r in caplog.records if "TRADE END" in r.getMessage()]
    assert len(end) == 1 and "failsafe: event > 5400" in end[0]


def test_trade_reverts_when_mqtt_lost_too_long(actuator, clock, timers, make_command):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    timers.fire_pending()
    ctrl.on_connected(False)
    clock.advance(350)
    ctrl.tick()
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


def test_shutdown_during_trade_reverts(actuator, clock, timers, make_command):
    ctrl = make_trade_controller(actuator)
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    timers.fire_pending()
    ctrl.shutdown()
    assert ctrl.state == "IDLE"
    assert ctrl.kind is None
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


def test_trade_start_and_end_logs_name_the_kind(actuator, clock, timers, make_command, caplog):
    ctrl = make_trade_controller(actuator)
    with caplog.at_level("INFO"):
        ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000, BatterySoc=100))
        timers.fire_pending()
        ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=0))
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("TRADE START") and "until SOC 100%" in m for m in msgs)
    assert any(m.startswith("TRADE END (qilowatt/buy 0 W)") for m in msgs)
    assert not any("mFRR START" in m or "mFRR END" in m for m in msgs)


def test_state_change_hook_fires_for_trades(actuator, clock, timers, make_command):
    events = []
    ctrl = make_trade_controller(actuator)
    ctrl.on_state_change = lambda state, signed: events.append((state, signed, ctrl.kind))
    ctrl.on_workmode(make_command(source="qilowatt", mode="buy", power=27000))
    assert events[-1] == ("ACTIVE", 27000, "trade")
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=15000))
    assert events[-1] == ("ACTIVE", -15000, "frr")
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=0))
    assert events[-1] == ("IDLE", 0, None)


# --------------------------------------------------------------------------- #
# State-change hook (local-bridge / Node-RED coexistence)
# --------------------------------------------------------------------------- #

def test_on_state_change_fires_on_enter_update_and_revert(
    actuator, clock, timers, make_command
):
    events = []
    ctrl = make_controller(actuator)
    ctrl.on_state_change = lambda state, signed: events.append((state, signed))

    # Enter: frrup export -> ACTIVE with negative signed watts.
    ctrl.on_workmode(make_command(source="fusebox", mode="frrup", power=5000))
    assert events[-1] == ("ACTIVE", -5000)

    # Setpoint update while ACTIVE -> another ACTIVE notification.
    ctrl.on_workmode(make_command(source="fusebox", mode="frrup", power=7000))
    assert events[-1] == ("ACTIVE", -7000)

    # Revert -> IDLE with signed 0.
    ctrl.on_workmode(make_command(source="grid", mode="normal", power=0))
    assert events[-1] == ("IDLE", 0)


def test_on_state_change_faulty_listener_does_not_break_machine(
    actuator, clock, timers, make_command
):
    def boom(state, signed):
        raise RuntimeError("listener blew up")

    ctrl = make_controller(actuator)
    ctrl.on_state_change = boom
    # Must still transition normally despite the raising listener.
    ctrl.on_workmode(make_command(source="fusebox", mode="frrdown", power=3000))
    assert ctrl.state == "ACTIVE"
    timers.fire_pending()
    assert actuator.setpoints == [3000]


# --------------------------------------------------------------------------- #
# Power gate: a zero-power FRR command is a stand-down, not a 0 W dispatch
#
# Live evidence (site A, 2026-07-03..27): 121 `kratt`/`frrup` commands carried
# PowerLimit=0. Held as an event they park DESS off and the SOC floor lowered
# while delivering nothing; 52 of them stranded the site until an unrelated
# later command happened to end the event (4.4 h total, worst 12.5 min).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["frrup", "frrdown"])
def test_zero_power_frr_does_not_open_an_event(
    actuator, clock, timers, make_command, mode
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="kratt", mode=mode, power=0))
    timers.fire_pending()
    assert ctrl.state == "IDLE"
    # Crucially no dess_off: DESS keeps doing arbitrage.
    assert actuator.calls == []


@pytest.mark.parametrize("mode", ["frrup", "frrdown"])
def test_zero_power_frr_ends_an_active_event(
    actuator, clock, timers, make_command, mode
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=5000))
    timers.fire_pending()
    assert ctrl.state == "ACTIVE"

    ctrl.on_workmode(make_command(source="kratt", mode=mode, power=0))
    assert ctrl.state == "IDLE"
    assert actuator.calls[-2:] == [("set_setpoint", 0), ("dess_on",)]


def test_stand_down_then_new_dispatch_starts_a_fresh_event(
    actuator, clock, timers, make_command
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=5000))
    timers.fire_pending()
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=0))
    assert ctrl.state == "IDLE"

    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=8000))
    timers.fire_pending()
    assert ctrl.state == "ACTIVE"
    assert actuator.setpoints[-1] == -8000
    # A fresh event must drop DESS again rather than assume it is still off.
    assert actuator.names().count("dess_off") == 2


# --------------------------------------------------------------------------- #
# The END line has to name its trigger
#
# Attributing an event end used to mean hand-matching timestamps between the
# agent log and the WORKMODE capture, which is how a foreign automation
# truncating mFRR events went unnoticed for a morning.
# --------------------------------------------------------------------------- #

def test_end_log_names_the_command_that_ended_the_event(
    actuator, clock, timers, make_command, caplog
):
    ctrl = make_controller(actuator)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=5000))
    timers.fire_pending()

    with caplog.at_level("INFO"):
        ctrl.on_workmode(make_command(source="optimizer", mode="limitexport", power=0))

    end = [r for r in caplog.records if "mFRR END" in r.getMessage()]
    assert len(end) == 1
    assert "optimizer/limitexport" in end[0].getMessage()


def test_end_log_names_the_failsafe_that_ended_the_event(
    actuator, clock, timers, make_command, caplog
):
    ctrl = make_controller(actuator, max_duration_s=100.0)
    ctrl.on_workmode(make_command(source="kratt", mode="frrup", power=5000))
    timers.fire_pending()

    clock.advance(101.0)
    with caplog.at_level("INFO"):
        ctrl.tick()

    end = [r for r in caplog.records if "mFRR END" in r.getMessage()]
    assert len(end) == 1
    assert "failsafe" in end[0].getMessage()
