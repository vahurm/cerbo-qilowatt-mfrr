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
    assert cfg.subscribe_grace_s == 120.0
    assert cfg.idle_refresh_s == 345600.0
    assert cfg.connect_attempts == 5
    assert cfg.connect_retry_s == 5.0
    assert cfg.mqtt_lost_failsafe_s == 300.0
    assert cfg.max_event_s == 7200.0
    # Q trades are honoured by default from the vendor's SOC optimiser only.
    assert cfg.trade_sources == ("qilowatt",)
    assert cfg.trade_modes == ("buy", "sell")
    assert cfg.max_trade_s == 5400.0
    # No agent-side cap unless the site limits are configured.
    assert cfg.max_import_w is None
    assert cfg.max_export_w is None


def test_max_event_cap_stays_below_the_dess_watchdog_backstop(monkeypatch):
    """The agent must always be the one that ends an event.

    qw_dess_watchdog.sh restores DESS (and the SOC floor) without touching the
    grid setpoint, so if it fires first the two failsafes disagree: arbitrage is
    back while mFRR still holds the inverter. Read the shell default straight
    from the script so the two cannot drift apart unnoticed.
    """
    import os
    import re

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "qw_dess_watchdog.sh",
    )
    with open(script, encoding="utf-8") as fh:
        m = re.search(r'MAX_OFF_SECS="\$\{QW_MAX_OFF_SECS:-(\d+)\}"', fh.read())
    assert m, "could not read QW_MAX_OFF_SECS default from qw_dess_watchdog.sh"

    watchdog_default = float(m.group(1))
    _set_environ(monkeypatch, dict(REQUIRED))
    cfg = qw_agent.Config()
    assert cfg.max_event_s < watchdog_default
    # The trade cap is a second event cap on the same DESS-off state and is
    # bound by the same ordering.
    assert cfg.max_trade_s < watchdog_default


def test_idle_refresh_default_clears_the_measured_command_silence(monkeypatch):
    """The idle-refresh backstop must not fire during genuine market quiet.

    A restart makes the portal push a snapshot ~20 s later, which resets this
    timer — so a default below a site's real gaps both restarts for nothing and
    erases the evidence needed to correct it. Site B ran that loop for weeks at
    the old 6 h default: 49 of its 70 starts were this watchdog, and its measured
    median gap came out as the setting itself. Site A then repeated it at the
    48 h default: 3 silences of exactly 48.0 h over 72.6 d (2026-09-14 probe).

    These are measured maxima (tools/afrr_probe.py over the durable WORKMODE
    capture), not estimates. Each is a lower bound, because the restart loop
    truncated every gap it did not cause.
    """
    measured_max_h = {"site A": 48.0, "site B": 31.9}

    _set_environ(monkeypatch, dict(REQUIRED))
    default_h = qw_agent.Config().idle_refresh_s / 3600.0

    worst = max(measured_max_h.values())
    assert default_h > worst
    assert default_h - worst >= 12.0, (
        f"only {default_h - worst:.1f}h of headroom over the longest measured "
        "silence; too little for a lower bound"
    )


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
            "QW_SUBSCRIBE_GRACE_S": "60",
            "QW_IDLE_REFRESH_S": "0",
            "QW_CONNECT_ATTEMPTS": "3",
            "QW_CONNECT_RETRY_S": "2.5",
            "QW_MFRR_SOURCES": "fusebox, kratt , extra",
            "QW_TRADE_SOURCES": "qilowatt, manual",
            "QW_TRADE_MODES": "buy",
            "QW_MAX_TRADE_S": "3600",
            "QW_MAX_IMPORT_W": "28000",
            "QW_MAX_EXPORT_W": "15000",
            "QW_DRY_RUN": "1",
        }
    )
    _set_environ(monkeypatch, env)
    cfg = qw_agent.Config()

    assert cfg.trade_sources == ("qilowatt", "manual")
    assert cfg.trade_modes == ("buy",)
    assert cfg.max_trade_s == 3600.0
    assert cfg.max_import_w == 28000.0
    assert cfg.max_export_w == 15000.0

    assert cfg.mqtt_port == 8884
    assert cfg.mqtt_tls is False
    assert cfg.telemetry_profile == "ac_coupled"
    assert cfg.export_limit_w == 12000.0
    assert cfg.mqtt_lost_failsafe_s == 120.0
    assert cfg.max_event_s == 900.0
    assert cfg.dess_off_delay_s == 3.0
    assert cfg.tick_interval_s == 5.0
    assert cfg.link_restart_s == 300.0
    assert cfg.subscribe_grace_s == 60.0
    assert cfg.idle_refresh_s == 0.0   # 0 disables the idle-refresh backstop
    assert cfg.connect_attempts == 3
    assert cfg.connect_retry_s == 2.5
    assert cfg.mfrr_sources == ("fusebox", "kratt", "extra")
    assert cfg.dry_run is True


def test_empty_trade_modes_disables_trades(monkeypatch):
    """QW_TRADE_MODES= (empty) is the documented way back to drop-all-trades."""
    env = dict(REQUIRED)
    env["QW_TRADE_MODES"] = ""
    _set_environ(monkeypatch, env)
    assert qw_agent.Config().trade_modes == ()


@pytest.mark.parametrize("raw", ["", "  ", "abc"])
def test_power_limits_unset_or_garbage_mean_no_cap(monkeypatch, raw):
    env = dict(REQUIRED)
    env["QW_MAX_IMPORT_W"] = raw
    env["QW_MAX_EXPORT_W"] = raw
    _set_environ(monkeypatch, env)
    cfg = qw_agent.Config()
    assert cfg.max_import_w is None
    assert cfg.max_export_w is None


def test_soc_reader_returns_none_without_dbus():
    """Off-Cerbo the reader must say 'unknown', never a fake 0 that ends a sell."""
    class _NoBus:
        available = False

        def get(self, *_a, **_k):
            raise AssertionError("must not be called when unavailable")

    assert qw_agent.make_soc_reader(_NoBus())() is None


@pytest.mark.parametrize("raw,expected", [(87.0, 87.0), ("42", 42.0), (None, None), ("n/a", None)])
def test_soc_reader_parses_dbus_value(raw, expected):
    class _Bus:
        available = True

        def get(self, service, path, default=None):
            assert service == "com.victronenergy.system" and path == "/Dc/Battery/Soc"
            return raw

    assert qw_agent.make_soc_reader(_Bus())() == expected


def test_config_missing_required_exits(monkeypatch):
    env = dict(REQUIRED)
    del env["QW_DEVICE_ID"]
    _set_environ(monkeypatch, env)
    with pytest.raises(SystemExit):
        qw_agent.Config()
