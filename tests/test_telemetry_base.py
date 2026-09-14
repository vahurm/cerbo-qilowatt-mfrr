"""Tests for the shared telemetry builders (agent/telemetry/base.py)."""

from __future__ import annotations

import pytest

from telemetry import base
from telemetry.base import _to_native, build_energy_data, build_metrics_data, volt

SYS = base.SVC_SYSTEM
SET = base.SVC_SETTINGS
VEBUS = "com.victronenergy.vebus.ttyS4"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "value,expected",
    [
        (230.5, 230.5),
        (400, 400.0),
        (49.9, 230.0),   # implausibly low -> nominal fallback
        (0.0, 230.0),
        (None, 230.0),
        ("garbage", 230.0),
    ],
)
def test_volt_fallback(value, expected):
    assert volt(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ([], None),         # empty dbus array = 'invalid' sentinel
        ((1, 2), None),
        (230, 230.0),
        (51.2, 51.2),
        ("abc", "abc"),     # non-numeric strings pass through unchanged
    ],
)
def test_to_native(value, expected):
    assert _to_native(value) == expected


# --------------------------------------------------------------------------- #
# build_energy_data
# --------------------------------------------------------------------------- #

def _energy_reader(extra=None, services=None):
    from conftest import FakeReader

    values = {
        (SYS, "/Ac/Grid/L1/Power"): 100,
        (SYS, "/Ac/Grid/L2/Power"): 200,
        (SYS, "/Ac/Grid/L3/Power"): 300,
        (SYS, "/Ac/Grid/L1/Current"): 1,
        (SYS, "/Ac/Grid/L2/Current"): 2,
        (SYS, "/Ac/Grid/L3/Current"): 3,
    }
    values.update(extra or {})
    return FakeReader(values=values, services=services or {})


def test_energy_reads_voltage_and_frequency_from_vebus():
    reader = _energy_reader(
        extra={
            (VEBUS, "/Ac/ActiveIn/L1/V"): 230.0,
            (VEBUS, "/Ac/ActiveIn/L2/V"): 231.0,
            (VEBUS, "/Ac/ActiveIn/L3/V"): 229.0,
            (VEBUS, "/Ac/ActiveIn/L1/F"): 50.01,
        },
        services={base.SVC_VEBUS_PREFIX: VEBUS},
    )
    e = build_energy_data(reader, 15000.0)
    assert e.Power == [100.0, 200.0, 300.0]
    assert e.Current == [1.0, 2.0, 3.0]
    assert e.Voltage == [230.0, 231.0, 229.0]
    assert e.Frequency == 50.01


def test_energy_frequency_below_40_falls_back_to_50():
    reader = _energy_reader(
        extra={(VEBUS, "/Ac/ActiveIn/L1/F"): 0.0},
        services={base.SVC_VEBUS_PREFIX: VEBUS},
    )
    e = build_energy_data(reader, 15000.0)
    assert e.Frequency == 50.0


def test_energy_without_vebus_uses_system_grid_voltage_and_nominal_freq():
    reader = _energy_reader(
        extra={
            (SYS, "/Ac/Grid/L1/Voltage"): 240.0,
            (SYS, "/Ac/Grid/L2/Voltage"): 240.0,
            (SYS, "/Ac/Grid/L3/Voltage"): 240.0,
        },
        services={},  # no vebus discovered
    )
    e = build_energy_data(reader, 15000.0)
    assert e.Voltage == [240.0, 240.0, 240.0]
    assert e.Frequency == 50.0


# --------------------------------------------------------------------------- #
# build_metrics_data
# --------------------------------------------------------------------------- #

def _metrics_reader(extra=None):
    from conftest import FakeReader

    values = {
        (SYS, "/Ac/Consumption/L1/Power"): 50,
        (SYS, "/Ac/Consumption/L2/Power"): 60,
        (SYS, "/Ac/Consumption/L3/Power"): 70,
        (SYS, "/Dc/Battery/Power"): -500,
        (SYS, "/Dc/Battery/Voltage"): 51.2,
        (SYS, "/Dc/Battery/Current"): -10,
        (SYS, "/Dc/Battery/Soc"): 55.4,
        (SYS, "/Dc/Battery/Temperature"): 25,
    }
    values.update(extra or {})
    return FakeReader(values=values)


def test_metrics_uses_configured_export_cap_when_feedin_unlimited():
    reader = _metrics_reader()  # MaxFeedInPower absent -> get_float default -1.0
    m = build_metrics_data(reader, 15000.0, lambda r: 1234.0)
    assert m.PvPower == [1234.0]
    assert m.LoadPower == [50.0, 60.0, 70.0]
    assert m.BatteryPower == [-500.0]
    assert m.BatteryVoltage == [51.2]
    assert m.BatterySOC == 55  # int, rounded from 55.4
    assert m.GridExportLimit == 15000.0


def test_metrics_uses_maxfeedin_when_set():
    reader = _metrics_reader(extra={(SET, base.PATH_MAX_FEEDIN): 8000.0})
    m = build_metrics_data(reader, 15000.0, lambda r: 0.0)
    assert m.GridExportLimit == 8000.0


def test_metrics_inverter_status_alarms_and_temperature():
    # Mirrors the official qilowatt-ha VictronInverter payload: constant status
    # 2, all-zero alarms, and battery temperature reported as inverter temp.
    reader = _metrics_reader(extra={(SYS, "/Dc/Battery/Temperature"): 25})
    m = build_metrics_data(reader, 15000.0, lambda r: 0.0)
    assert m.InverterStatus == 2
    assert m.AlarmCodes == [0, 0, 0, 0, 0, 0]
    assert m.BatteryTemperature == [25.0]
    assert m.InverterTemperature == 25.0


# --------------------------------------------------------------------------- #
# ENERGY.Today / Total from the grid meter
# --------------------------------------------------------------------------- #

GRID = "com.victronenergy.grid.cgwacs_ttyUSB0_di30_mb1"


def test_energy_totals_zero_without_grid_service(tmp_path):
    daily = base.DailyEnergy(str(tmp_path / "e.json"))
    e = build_energy_data(_energy_reader(), 15000.0, daily=daily)
    assert e.Today == 0.0 and e.Total == 0.0


def test_energy_total_is_grid_import_and_today_is_since_midnight(tmp_path):
    now = [1_700_000_000.0]
    daily = base.DailyEnergy(str(tmp_path / "e.json"), now=lambda: now[0])
    reader = _energy_reader(
        extra={(GRID, "/Ac/Energy/Forward"): 1234.5, (GRID, "/Ac/Energy/Reverse"): 99.0},
        services={base.SVC_GRID_PREFIX: GRID},
    )
    e = build_energy_data(reader, 15000.0, daily=daily)
    assert e.Total == 1234.5
    assert e.Today == 0.0          # first sample of the day is the baseline

    reader.values[(GRID, "/Ac/Energy/Forward")] = 1240.0
    e = build_energy_data(reader, 15000.0, daily=daily)
    assert e.Today == 5.5


def test_daily_baseline_survives_restart_and_rolls_at_midnight(tmp_path):
    path = str(tmp_path / "e.json")
    now = [1_700_000_000.0]
    d1 = base.DailyEnergy(path, now=lambda: now[0])
    assert d1.today(100.0) == 0.0
    assert d1.today(103.0) == 3.0

    d2 = base.DailyEnergy(path, now=lambda: now[0])   # "restart"
    assert d2.today(104.0) == 4.0

    now[0] += 86400.0                                   # next day
    assert d2.today(110.0) == 0.0
    assert d2.today(111.5) == 1.5


def test_daily_rebases_when_counter_goes_backwards(tmp_path):
    d = base.DailyEnergy(str(tmp_path / "e.json"), now=lambda: 1_700_000_000.0)
    assert d.today(500.0) == 0.0
    assert d.today(10.0) == 0.0     # meter replaced / reset
    assert d.today(12.0) == 2.0


def test_grid_energy_kwh_handles_missing_and_invalid():
    assert base.grid_energy_kwh(_energy_reader()) == (None, None)
    reader = _energy_reader(
        extra={(GRID, "/Ac/Energy/Forward"): "x"}, services={base.SVC_GRID_PREFIX: GRID}
    )
    assert base.grid_energy_kwh(reader) == (None, None)
