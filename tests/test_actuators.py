"""Tests for agent/actuators.py (ScriptActuator argv + DryRunActuator no-ops)."""

from __future__ import annotations

import types

import pytest

import actuators


class _Recorder:
    def __init__(self, returncode=0, exc=None):
        self.calls: list = []
        self._returncode = returncode
        self._exc = exc

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self._exc is not None:
            raise self._exc
        return types.SimpleNamespace(returncode=self._returncode, stdout="ok", stderr="")


@pytest.fixture
def actuator():
    return actuators.ScriptActuator(
        dess_script="/data/qw_dess_toggle.sh",
        setpoint_script="/data/qw_grid_setpoint.sh",
    )


def test_dess_off_and_on_argv(monkeypatch, actuator):
    rec = _Recorder()
    monkeypatch.setattr(actuators.subprocess, "run", rec)

    actuator.dess_off()
    actuator.dess_on()

    assert rec.calls == [
        ["/data/qw_dess_toggle.sh", "off"],
        ["/data/qw_dess_toggle.sh", "on"],
    ]


def test_set_setpoint_argv_positive_and_negative(monkeypatch, actuator):
    rec = _Recorder()
    monkeypatch.setattr(actuators.subprocess, "run", rec)

    actuator.set_setpoint(3000)
    actuator.set_setpoint(-15000)

    assert rec.calls == [
        ["/data/qw_grid_setpoint.sh", "3000"],
        ["/data/qw_grid_setpoint.sh", "-15000"],
    ]


def test_set_setpoint_coerces_float_to_int(monkeypatch, actuator):
    rec = _Recorder()
    monkeypatch.setattr(actuators.subprocess, "run", rec)

    actuator.set_setpoint(3000.0)
    assert rec.calls == [["/data/qw_grid_setpoint.sh", "3000"]]


def test_nonzero_exit_is_logged_not_raised(monkeypatch, actuator):
    rec = _Recorder(returncode=3)
    monkeypatch.setattr(actuators.subprocess, "run", rec)

    actuator.set_setpoint(99999)  # rejected by script -> exit 3, but no exception
    assert rec.calls == [["/data/qw_grid_setpoint.sh", "99999"]]


def test_subprocess_exception_is_swallowed(monkeypatch, actuator):
    rec = _Recorder(exc=OSError("boom"))
    monkeypatch.setattr(actuators.subprocess, "run", rec)

    actuator.dess_off()  # must not propagate the OSError


def test_dry_run_actuator_is_inert():
    dry = actuators.DryRunActuator()
    dry.dess_off()
    dry.dess_on()
    dry.set_setpoint(-15000)  # no exception, no side effects
