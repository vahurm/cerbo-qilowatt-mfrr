"""Tests for qw_agent.TelemetryLoop's no-dbus guard (A3) and state.json."""

from __future__ import annotations

import json
import types

import qw_agent
from conftest import FakeReader
from telemetry import auto, base


class _Device:
    def __init__(self):
        self.energy = []
        self.metrics = []

    def set_energy_data(self, e):
        self.energy.append(e)

    def set_metrics_data(self, m):
        self.metrics.append(m)


def _loop(reader):
    cfg = types.SimpleNamespace(export_limit_w=15000.0, telemetry_interval_s=5.0)
    dev = _Device()
    return qw_agent.TelemetryLoop(cfg, dev, auto, reader=reader), dev


def test_publishes_when_soc_readable():
    reader = FakeReader(values={(base.SVC_SYSTEM, "/Dc/Battery/Soc"): 55.0})
    loop, dev = _loop(reader)
    assert loop.update_once() is True
    assert len(dev.energy) == 1 and len(dev.metrics) == 1
    assert dev.metrics[0].BatterySOC == 55
    assert loop.published == 1 and loop.skipped == 0


def test_no_dbus_publishes_nothing_and_logs_once_per_minute(monkeypatch, caplog):
    reader = FakeReader()
    reader.available = False
    loop, dev = _loop(reader)

    t = [1000.0]
    monkeypatch.setattr(qw_agent.time, "monotonic", lambda: t[0])
    with caplog.at_level("ERROR"):
        for _ in range(5):
            assert loop.update_once() is False
            t[0] += 5.0
    assert dev.energy == [] and dev.metrics == []
    assert loop.skipped == 5
    msgs = [r.message for r in caplog.records if "telemetry unavailable" in r.message]
    assert len(msgs) == 1 and "dbus not available" in msgs[0]

    t[0] += 60.0
    with caplog.at_level("ERROR"):
        loop.update_once()
    msgs = [r.message for r in caplog.records if "telemetry unavailable" in r.message]
    assert len(msgs) == 2


def test_dbus_up_but_soc_missing_publishes_nothing(caplog):
    """D-Bus reachable but the system service has no SOC (BMS gone): no SENSOR."""
    reader = FakeReader(values={(base.SVC_SYSTEM, "/Ac/Grid/L1/Power"): 500.0})
    loop, dev = _loop(reader)
    with caplog.at_level("ERROR"):
        assert loop.update_once() is False
    assert dev.metrics == []
    assert any("/Dc/Battery/Soc unreadable" in r.message for r in caplog.records)


def test_recovery_is_logged_and_publishing_resumes(caplog):
    reader = FakeReader()
    loop, dev = _loop(reader)
    loop.update_once()  # SOC missing -> skipped
    reader.values[(base.SVC_SYSTEM, "/Dc/Battery/Soc")] = 80.0
    with caplog.at_level("INFO"):
        assert loop.update_once() is True
    assert any("telemetry available again" in r.message for r in caplog.records)
    assert len(dev.metrics) == 1


# --------------------------------------------------------------------------- #
# state.json
# --------------------------------------------------------------------------- #

def test_state_file_written_atomically(tmp_path):
    ctrl = types.SimpleNamespace(state="ACTIVE", kind="frr", last_signed_watts=-5000, degraded=False)
    path = tmp_path / "sub" / "state.json"
    qw_agent.write_state_file(str(path), ctrl, extra={"site": "x"})
    data = json.loads(path.read_text())
    assert data["state"] == "ACTIVE" and data["kind"] == "frr" and data["signed_w"] == -5000
    assert data["degraded"] is False and data["site"] == "x"
    assert data["version"] == qw_agent.__version__
    assert not (tmp_path / "sub" / "state.json.tmp").exists()


def test_state_file_empty_path_is_noop():
    ctrl = types.SimpleNamespace(state="IDLE", kind=None, last_signed_watts=0, degraded=False)
    qw_agent.write_state_file("", ctrl)  # no exception


def test_read_install_version(tmp_path):
    p = tmp_path / "VERSION"
    assert qw_agent.read_install_version(str(p)) == "-"
    p.write_text("1.0.0+abc123\n")
    assert qw_agent.read_install_version(str(p)) == "1.0.0+abc123"
