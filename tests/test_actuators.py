"""Tests for agent/actuators.py — argv, read-back verification, dry-run."""

from __future__ import annotations

import types

import pytest

import actuators


class _FakeScripts:
    """Simulates qw_grid_setpoint.sh / qw_dess_toggle.sh incl. their read-backs.

    ``setpoint`` and ``dess_mode`` are the "live registers"; a write updates
    them unless the value is in ``reject`` (script exit 3, register untouched)
    or ``stuck`` is set (write reports success but the register does not move —
    the silent-dbus-failure case).
    """

    def __init__(self, setpoint=0, dess_mode=1, reject=(), stuck=False, exc=None):
        self.calls: list = []
        self.setpoint = setpoint
        self.dess_mode = dess_mode
        self.reject = set(reject)
        self.stuck = stuck
        self.exc = exc

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.exc is not None:
            raise self.exc
        script, arg = cmd[0], cmd[1]
        if script.endswith("qw_grid_setpoint.sh"):
            if arg == "get":
                return self._ok("AcPowerSetPoint = %s W" % self.setpoint)
            val = int(arg)
            if val in self.reject:
                return self._fail(3, "ERROR: limit exceeded")
            old = self.setpoint
            if not self.stuck:
                self.setpoint = val
            return self._ok("AcPowerSetPoint: %s -> %s W" % (old, self.setpoint))
        if script.endswith("qw_dess_toggle.sh"):
            if arg == "status":
                return self._ok(
                    "DESS Mode (live)    = %s\nSaved Mode          = 1\n" % self.dess_mode
                )
            if arg == "off":
                if not self.stuck:
                    self.dess_mode = 0
                return self._ok("DESS Mode 1 -> 0")
            if arg == "on":
                if not self.stuck:
                    self.dess_mode = 1
                return self._ok("DESS Mode 0 -> 1")
        return self._fail(1, "unknown")

    @staticmethod
    def _ok(out):
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    @staticmethod
    def _fail(code, err):
        return types.SimpleNamespace(returncode=code, stdout="", stderr=err)

    def writes(self):
        return [c for c in self.calls if c[1] not in ("get", "status")]


@pytest.fixture
def actuator():
    return actuators.ScriptActuator(
        dess_script="/data/qw_dess_toggle.sh",
        setpoint_script="/data/qw_grid_setpoint.sh",
    )


@pytest.fixture
def scripts(monkeypatch):
    fake = _FakeScripts()
    monkeypatch.setattr(actuators.subprocess, "run", fake)
    return fake


# --------------------------------------------------------------------------- #
# argv
# --------------------------------------------------------------------------- #

def test_dess_off_and_on_argv(scripts, actuator):
    assert actuator.dess_off() is True
    assert actuator.dess_on() is True
    assert scripts.writes() == [
        ["/data/qw_dess_toggle.sh", "off"],
        ["/data/qw_dess_toggle.sh", "on"],
    ]


def test_dess_off_without_floor_passes_the_flag(scripts, actuator):
    actuator.dess_off(lower_floor=False)
    actuator.dess_off(lower_floor=True)
    assert scripts.writes() == [
        ["/data/qw_dess_toggle.sh", "off", "--no-floor"],
        ["/data/qw_dess_toggle.sh", "off"],
    ]


def test_set_setpoint_argv_positive_and_negative(scripts, actuator):
    assert actuator.set_setpoint(3000) is True
    assert actuator.set_setpoint(-15000) is True
    assert scripts.writes() == [
        ["/data/qw_grid_setpoint.sh", "3000"],
        ["/data/qw_grid_setpoint.sh", "-15000"],
    ]


def test_set_setpoint_coerces_float_to_int(scripts, actuator):
    actuator.set_setpoint(3000.0)
    assert scripts.writes() == [["/data/qw_grid_setpoint.sh", "3000"]]


# --------------------------------------------------------------------------- #
# read-back verification (A2)
# --------------------------------------------------------------------------- #

def test_setpoint_write_is_read_back(scripts, actuator):
    actuator.set_setpoint(3000)
    # write, then `get`
    assert scripts.calls == [
        ["/data/qw_grid_setpoint.sh", "3000"],
        ["/data/qw_grid_setpoint.sh", "get"],
    ]


def test_rejected_setpoint_returns_false_and_skips_readback(monkeypatch, actuator):
    fake = _FakeScripts(reject={99999})
    monkeypatch.setattr(actuators.subprocess, "run", fake)
    assert actuator.set_setpoint(99999) is False  # exit 3, no exception
    assert fake.calls == [["/data/qw_grid_setpoint.sh", "99999"]]


def test_setpoint_that_does_not_stick_returns_false(monkeypatch, actuator):
    fake = _FakeScripts(setpoint=0, stuck=True)
    monkeypatch.setattr(actuators.subprocess, "run", fake)
    assert actuator.set_setpoint(-5000) is False


def test_dess_off_that_does_not_stick_returns_false(monkeypatch, actuator):
    fake = _FakeScripts(dess_mode=1, stuck=True)
    monkeypatch.setattr(actuators.subprocess, "run", fake)
    assert actuator.dess_off() is False


def test_dess_on_that_does_not_stick_returns_false(monkeypatch, actuator):
    fake = _FakeScripts(dess_mode=0, stuck=True)
    monkeypatch.setattr(actuators.subprocess, "run", fake)
    assert actuator.dess_on() is False


def test_unreadable_readback_is_assumed_ok(monkeypatch, actuator):
    """A site whose `status`/`get` output cannot be parsed is not failed."""
    class _NoReadback(_FakeScripts):
        def __call__(self, cmd, **kwargs):
            if cmd[1] in ("get", "status"):
                self.calls.append(cmd)
                return self._ok("")
            return super().__call__(cmd, **kwargs)

    monkeypatch.setattr(actuators.subprocess, "run", _NoReadback())
    assert actuator.set_setpoint(100) is True
    assert actuator.dess_off() is True


def test_verify_can_be_disabled(monkeypatch):
    fake = _FakeScripts(stuck=True)
    monkeypatch.setattr(actuators.subprocess, "run", fake)
    act = actuators.ScriptActuator(
        "/data/qw_dess_toggle.sh", "/data/qw_grid_setpoint.sh", verify=False
    )
    assert act.set_setpoint(100) is True
    assert fake.calls == [["/data/qw_grid_setpoint.sh", "100"]]


def test_subprocess_exception_is_swallowed_and_returns_false(monkeypatch, actuator):
    monkeypatch.setattr(actuators.subprocess, "run", _FakeScripts(exc=OSError("boom")))
    assert actuator.dess_off() is False  # must not propagate the OSError


def test_read_helpers(scripts, actuator):
    scripts.setpoint = -1234
    scripts.dess_mode = 1
    assert actuator.read_setpoint() == -1234.0
    assert actuator.read_dess_mode() == 1.0


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #

def test_dry_run_actuator_is_inert_and_reports_success():
    dry = actuators.DryRunActuator()
    assert dry.dess_off() is True
    assert dry.dess_off(lower_floor=False) is True
    assert dry.dess_on() is True
    assert dry.set_setpoint(-15000) is True  # no exception, no side effects
