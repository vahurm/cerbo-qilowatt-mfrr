"""Tests for env/config parsing in agent/qw_agent.py."""

from __future__ import annotations

import pytest

import qw_agent

REQUIRED = {
    "QW_DEVICE_ID": "00000000-0000-0000-0000-000000000000",
    "QW_MQTT_USER": "user",
    "QW_MQTT_PASS": "pass",
}


def _set_environ(monkeypatch, env: dict):
    """Replace qw_agent's view of os.environ with the given dict (same object,
    so a test can inspect mutations such as _load_env_file's setdefault)."""
    monkeypatch.setattr(qw_agent.os, "environ", env)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def test_env_bool(monkeypatch):
    _set_environ(monkeypatch, {"A": "1", "B": "0"})
    assert qw_agent._env_bool("A", False) is True
    assert qw_agent._env_bool("B", True) is False
    assert qw_agent._env_bool("MISSING", True) is True
    assert qw_agent._env_bool("MISSING", False) is False


def test_require_returns_value(monkeypatch):
    _set_environ(monkeypatch, {"QW_DEVICE_ID": "abc"})
    assert qw_agent._require("QW_DEVICE_ID") == "abc"


def test_require_missing_raises(monkeypatch):
    _set_environ(monkeypatch, {})
    with pytest.raises(SystemExit):
        qw_agent._require("QW_DEVICE_ID")


def test_require_rejects_placeholder(monkeypatch):
    _set_environ(monkeypatch, {"QW_DEVICE_ID": "REPLACE_WITH_INVERTER_ID"})
    with pytest.raises(SystemExit):
        qw_agent._require("QW_DEVICE_ID")


def test_load_env_file_setdefault_semantics(monkeypatch, tmp_path):
    env_file = tmp_path / "qw-agent.env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                'QW_DEVICE_ID="quoted-value"',
                "QW_MQTT_USER = spaced ",
                "PRESET=from_file",          # must NOT override existing
                "NO_EQUALS_LINE",            # ignored
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = {"PRESET": "already_set"}
    _set_environ(monkeypatch, env)

    qw_agent._load_env_file(str(env_file))

    assert env["QW_DEVICE_ID"] == "quoted-value"   # quotes stripped
    assert env["QW_MQTT_USER"] == "spaced"          # whitespace trimmed
    assert env["PRESET"] == "already_set"           # setdefault: not overridden
    assert "NO_EQUALS_LINE" not in env


def test_load_env_file_missing_path_is_noop(monkeypatch):
    _set_environ(monkeypatch, {})
    qw_agent._load_env_file("/no/such/file.env")  # must not raise


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def test_config_defaults(monkeypatch):
    _set_environ(monkeypatch, dict(REQUIRED))
    cfg = qw_agent.Config()

    assert cfg.device_id == REQUIRED["QW_DEVICE_ID"]
    assert cfg.mqtt_host == "mqtt.qilowatt.it"
    assert cfg.mqtt_port == 8883
    assert cfg.mqtt_tls is True
    assert cfg.telemetry_profile == "dc_coupled"
    assert cfg.export_limit_w == 15000.0
    assert cfg.mfrr_sources == ("fusebox", "kratt")
    assert cfg.dry_run is False
    assert cfg.local_bridge is False
    assert cfg.link_restart_s == 600.0


def test_config_overrides(monkeypatch):
    env = dict(REQUIRED)
    env.update(
        {
            "QW_MQTT_PORT": "8884",
            "QW_MQTT_TLS": "0",
            "QW_TELEMETRY_PROFILE": "ac_coupled",
            "QW_GRID_EXPORT_LIMIT_W": "12000",
            "QW_MQTT_LOST_FAILSAFE_S": "120",
            "QW_MAX_EVENT_S": "900",
            "QW_DESS_OFF_DELAY_S": "3",
            "QW_TICK_INTERVAL_S": "5",
            "QW_LINK_RESTART_S": "300",
            "QW_MFRR_SOURCES": "fusebox, kratt , extra",
            "QW_DRY_RUN": "1",
        }
    )
    _set_environ(monkeypatch, env)
    cfg = qw_agent.Config()

    assert cfg.mqtt_port == 8884
    assert cfg.mqtt_tls is False
    assert cfg.telemetry_profile == "ac_coupled"
    assert cfg.export_limit_w == 12000.0
    assert cfg.mqtt_lost_failsafe_s == 120.0
    assert cfg.max_event_s == 900.0
    assert cfg.dess_off_delay_s == 3.0
    assert cfg.tick_interval_s == 5.0
    assert cfg.link_restart_s == 300.0
    assert cfg.mfrr_sources == ("fusebox", "kratt", "extra")
    assert cfg.dry_run is True


def test_config_missing_required_exits(monkeypatch):
    env = dict(REQUIRED)
    del env["QW_DEVICE_ID"]
    _set_environ(monkeypatch, env)
    with pytest.raises(SystemExit):
        qw_agent.Config()
