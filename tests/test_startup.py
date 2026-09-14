"""Tests for agent/startup.py — leftover-event recovery and config warnings."""

from __future__ import annotations

import re
import time
import types

import pytest

import startup
from conftest import FakeActuator, FakeReader

_REPO_SCRIPTS = __import__("os").path.join(
    __import__("os").path.dirname(__import__("os").path.dirname(__file__)), "scripts"
)


# --------------------------------------------------------------------------- #
# A1: startup recovery
# --------------------------------------------------------------------------- #

def test_clean_start_does_nothing(tmp_path):
    act = FakeActuator()
    assert startup.recover_leftover_event(act, str(tmp_path), str(tmp_path / "off_at")) is False
    assert act.calls == []


def test_saved_mode_with_dess_live_off_triggers_setpoint_zero_then_dess_on(tmp_path, caplog):
    """The reboot case: /tmp stamp gone, saved Mode on /data, DESS still 0."""
    (tmp_path / startup.SAVED_MODE_FILE).write_text("1\n")
    act = FakeActuator()
    with caplog.at_level("WARNING"):
        assert startup.recover_leftover_event(
            act, str(tmp_path), str(tmp_path / "off_at"), dess_mode=0.0
        ) is True
    # Release the setpoint BEFORE restoring DESS so they don't fight.
    assert act.calls == [("set_setpoint", 0), ("dess_on",)]
    assert any(
        "STARTUP RECOVERY" in r.message and "saved DESS Mode 1 (live Mode 0)" in r.message
        for r in caplog.records
    )


def test_saved_mode_with_unreadable_dess_mode_recovers_conservatively(tmp_path):
    (tmp_path / startup.SAVED_MODE_FILE).write_text("1")
    act = FakeActuator()
    assert startup.recover_leftover_event(act, str(tmp_path), str(tmp_path / "off_at"), dess_mode=None) is True
    assert act.names() == ["set_setpoint", "dess_on"]


def test_stale_saved_mode_with_dess_on_is_removed_not_recovered(tmp_path, caplog):
    """Left by a toggle script that did not clean up on `on`: DESS is on, no stamp."""
    saved = tmp_path / startup.SAVED_MODE_FILE
    saved.write_text("1")
    act = FakeActuator()
    with caplog.at_level("INFO"):
        assert startup.recover_leftover_event(act, str(tmp_path), str(tmp_path / "off_at"), dess_mode=1.0) is False
    assert act.calls == []
    assert not saved.exists()
    assert any("removing stale" in r.message for r in caplog.records)


def test_saved_mode_plus_stamp_recovers_even_if_dess_reads_on(tmp_path):
    """Stamp present = `on` never ran; trust the files over a racing read."""
    (tmp_path / startup.SAVED_MODE_FILE).write_text("1")
    (tmp_path / "off_at").write_text(str(int(time.time())))
    act = FakeActuator()
    assert startup.recover_leftover_event(act, str(tmp_path), str(tmp_path / "off_at"), dess_mode=1.0) is True


def test_off_at_stamp_alone_triggers_recovery_with_age(tmp_path, caplog):
    """Reboot case inverted: /tmp stamp present, /data file gone (or vice versa)."""
    off_at = tmp_path / "off_at"
    off_at.write_text(str(int(time.time()) - 120))
    act = FakeActuator()
    with caplog.at_level("WARNING"):
        assert startup.recover_leftover_event(act, str(tmp_path), str(off_at)) is True
    assert act.names() == ["set_setpoint", "dess_on"]
    msg = next(r.message for r in caplog.records if "STARTUP RECOVERY" in r.message)
    assert re.search(r"DESS off for 1[12]\ds", msg)


def test_saved_floor_alone_is_evidence(tmp_path):
    """The floor is still lowered even if DESS reads on: `on` restores it."""
    (tmp_path / startup.SAVED_MINSOC_FILE).write_text("40")
    found = startup.leftover_event(str(tmp_path), str(tmp_path / "off_at"), dess_mode=1.0)
    assert found == "saved SOC floor 40%"
    act = FakeActuator()
    assert startup.recover_leftover_event(act, str(tmp_path), str(tmp_path / "off_at"), dess_mode=1.0) is True


def test_dess_off_without_any_file_is_the_owners_choice(tmp_path):
    act = FakeActuator()
    assert startup.recover_leftover_event(act, str(tmp_path), str(tmp_path / "off_at"), dess_mode=0.0) is False
    assert act.calls == []


def test_read_dess_mode():
    reader = FakeReader(values={(startup.SVC_SETTINGS, startup.PATH_DESS_MODE): 1.0})
    assert startup.read_dess_mode(reader) == 1.0
    assert startup.read_dess_mode(FakeReader()) is None
    reader.available = False
    assert startup.read_dess_mode(reader) is None
    assert startup.read_dess_mode(None) is None


def test_failed_recovery_is_logged_as_actuation_failed(tmp_path, caplog):
    (tmp_path / startup.SAVED_MODE_FILE).write_text("1")

    class _Failing(FakeActuator):
        def dess_on(self):
            super().dess_on()
            return False

    with caplog.at_level("ERROR"):
        startup.recover_leftover_event(_Failing(), str(tmp_path), str(tmp_path / "off_at"))
    assert any("actuation failed" in r.message for r in caplog.records)


def test_dry_run_actuator_only_logs(tmp_path):
    import actuators

    (tmp_path / startup.SAVED_MODE_FILE).write_text("1")
    assert startup.recover_leftover_event(
        actuators.DryRunActuator(), str(tmp_path), str(tmp_path / "off_at")
    ) is True


# --------------------------------------------------------------------------- #
# Constants pinned to the shell scripts
# --------------------------------------------------------------------------- #

def _script(name: str) -> str:
    with open(__import__("os").path.join(_REPO_SCRIPTS, name), encoding="utf-8") as fh:
        return fh.read()


def test_watchdog_default_matches_script():
    m = re.search(r'MAX_OFF_SECS="\$\{QW_MAX_OFF_SECS:-(\d+)\}"', _script("qw_dess_watchdog.sh"))
    assert m and float(m.group(1)) == startup.DEFAULT_WATCHDOG_MAX_OFF_S


def test_state_file_names_match_toggle_script():
    body = _script("qw_dess_toggle.sh")
    assert 'SAVED_MODE_FILE="${QW_STATE_DIR}/%s"' % startup.SAVED_MODE_FILE in body
    assert 'SAVED_MINSOC_FILE="${QW_STATE_DIR}/%s"' % startup.SAVED_MINSOC_FILE in body
    assert 'OFF_AT_FILE="%s"' % startup.DEFAULT_OFF_AT_FILE in body
    assert 'QW_STATE_DIR="${QW_STATE_DIR:-%s}"' % startup.DEFAULT_STATE_DIR in body


def test_setpoint_script_default_limit_matches():
    body = _script("qw_grid_setpoint.sh")
    assert re.search(
        r'MAX_IMPORT_W="\$\{QW_MAX_IMPORT_W:-%d\}"' % startup.SCRIPT_DEFAULT_LIMIT_W, body
    )
    assert re.search(
        r'MAX_EXPORT_W="\$\{QW_MAX_EXPORT_W:-%d\}"' % startup.SCRIPT_DEFAULT_LIMIT_W, body
    )


# --------------------------------------------------------------------------- #
# A4: config warnings
# --------------------------------------------------------------------------- #

def _cfg(**over):
    base = dict(
        max_event_s=7200.0, max_trade_s=5400.0,
        max_import_w=28000.0, max_export_w=15000.0,
        trade_sources=("qilowatt",), trade_modes=("buy", "sell"),
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_good_config_has_no_warnings():
    assert startup.config_warnings(_cfg(), reader=None, env={}) == []


def test_event_cap_at_or_above_watchdog_warns():
    warns = startup.config_warnings(_cfg(max_event_s=7800.0), env={})
    assert len(warns) == 1 and "QW_MAX_EVENT_S=7800" in warns[0] and "watchdog" in warns[0]


def test_watchdog_cap_from_env_is_honoured():
    warns = startup.config_warnings(_cfg(max_trade_s=5400.0), env={"QW_MAX_OFF_SECS": "5000"})
    assert any("QW_MAX_TRADE_S=5400" in w for w in warns)
    assert startup.config_warnings(_cfg(), env={"QW_MAX_OFF_SECS": "9000"}) == []


def test_missing_caps_warn():
    warns = startup.config_warnings(_cfg(max_import_w=None), env={})
    assert len(warns) == 1 and "not set" in warns[0] and "15000" in warns[0]


def test_example_default_caps_warn():
    warns = startup.config_warnings(_cfg(max_import_w=15000.0, max_export_w=15000.0), env={})
    assert len(warns) == 1 and "example value" in warns[0]


def test_min_soc_not_below_live_floor_warns():
    reader = FakeReader(values={(startup.SVC_SETTINGS, startup.PATH_MIN_SOC): 20.0})
    warns = startup.config_warnings(_cfg(), reader=reader, env={"QW_MFRR_MIN_SOC": "20"})
    assert len(warns) == 1 and "QW_MFRR_MIN_SOC=20" in warns[0]
    assert startup.config_warnings(_cfg(), reader=reader, env={"QW_MFRR_MIN_SOC": "10"}) == []


def test_min_soc_without_dbus_is_not_checked():
    reader = FakeReader()
    reader.available = False
    assert startup.config_warnings(_cfg(), reader=reader, env={"QW_MFRR_MIN_SOC": "99"}) == []


def test_non_numeric_min_soc_warns():
    warns = startup.config_warnings(_cfg(), env={"QW_MFRR_MIN_SOC": "ten"})
    assert len(warns) == 1 and "not a number" in warns[0]


def test_disabled_trades_warn():
    warns = startup.config_warnings(_cfg(trade_modes=()), env={})
    assert len(warns) == 1 and "QW_TRADE_MODES is empty" in warns[0]
