"""SENSOR WORKMODE echo follows the applied state, not the raw command."""

from __future__ import annotations

import types

from qilowatt import EnergyData, MetricsData, WorkModeCommand

from reported_workmode import ReportingInverterDevice, WorkmodeReporter, reported_workmode


def test_idle_is_normal_with_measured_soc():
    assert reported_workmode("IDLE", None, 0, 100.0) == {
        "Mode": "normal",
        "PowerLimit": 0,
        "BatterySoc": 100,
    }


def test_soc_is_rounded_and_missing_soc_is_omitted():
    assert reported_workmode("IDLE", None, 0, 99.6)["BatterySoc"] == 100
    assert "BatterySoc" not in reported_workmode("IDLE", None, 0, None)
    assert "BatterySoc" not in reported_workmode("IDLE", None, 0, "nope")


def test_zero_power_event_is_not_held():
    """A stand-down the machine has already released must not stay frrup."""
    assert reported_workmode("IDLE", None, 0, 100)["Mode"] == "normal"
    assert reported_workmode("ACTIVE", "frr", 0, 80)["Mode"] == "normal"
    assert reported_workmode("ACTIVE", "frr", 0, 80)["PowerLimit"] == 0


def test_active_frr_direction_and_magnitude():
    up = reported_workmode("ACTIVE", "frr", -3500, 92.2)
    assert up == {"Mode": "frrup", "PowerLimit": 3500, "BatterySoc": 92}
    down = reported_workmode("ACTIVE", "frr", 2700, 98)
    assert down["Mode"] == "frrdown" and down["PowerLimit"] == 2700


def test_active_trade_direction():
    buy = reported_workmode("ACTIVE", "trade", 20000, 40)
    sell = reported_workmode("ACTIVE", "trade", -5000, 90)
    assert buy["Mode"] == "buy" and buy["PowerLimit"] == 20000
    assert sell["Mode"] == "sell" and sell["PowerLimit"] == 5000


def test_unknown_kind_does_not_invent_a_mode():
    got = reported_workmode("ACTIVE", "other", -1000, 50)
    assert got["Mode"] == "normal" and got["PowerLimit"] == 0


def test_inbound_command_is_not_stored_before_the_agent_decides():
    dev = ReportingInverterDevice("dev")
    dev.set_command_callback(lambda _cmd: None)
    dev.handle_command(
        b'WORKMODE {"Mode":"frrup","_source":"kratt","BatterySoc":6,"PowerLimit":0}'
    )
    echoed = dev._workmode_command.to_dict()
    assert echoed["Mode"] == "normal"
    assert "BatterySoc" not in echoed


def test_callback_can_replace_the_echo_before_the_next_sensor():
    dev = ReportingInverterDevice("dev")

    def on_command(_cmd):
        dev.set_reported_workmode(
            WorkModeCommand.from_dict(reported_workmode("IDLE", None, 0, 100))
        )

    dev.set_command_callback(on_command)
    dev.handle_command(
        b'WORKMODE {"Mode":"frrup","BatterySoc":6,"PowerLimit":0}'
    )
    # Already initialised: setting data must not start the library's publish timers.
    dev._data_initialized = True
    dev.set_energy_data(EnergyData(
        Power=[0.0, 0.0, 0.0], Today=0.0, Total=0.0,
        Current=[0.0, 0.0, 0.0], Voltage=[230.0, 230.0, 230.0], Frequency=50.0,
    ))
    dev.set_metrics_data(MetricsData(
        PvPower=[0.0], PvVoltage=[0.0], PvCurrent=[0.0],
        LoadPower=[0.0, 0.0, 0.0], BatterySOC=[100], LoadCurrent=[0.0, 0.0, 0.0],
        BatteryPower=[0.0], BatteryCurrent=[0.0], BatteryVoltage=[52.0],
        GenVoltage=[0.0, 0.0, 0.0], GenPower=[0.0, 0.0, 0.0], GenCurrent=[0.0, 0.0, 0.0],
        GridExportLimit=15000.0, BatteryTemperature=[22.0], InverterTemperature=22.0,
    ))
    workmode = dev.get_sensor_data()["WORKMODE"]
    assert workmode["Mode"] == "normal"
    assert workmode["PowerLimit"] == 0
    assert workmode["BatterySoc"] == 100


def test_reporter_logs_mode_changes_only(caplog):
    dev = ReportingInverterDevice("dev")
    ctrl = types.SimpleNamespace(state="IDLE", kind=None, last_signed_watts=0)
    soc = {"v": 100.0}
    reporter = WorkmodeReporter(dev, ctrl, lambda: soc["v"])
    with caplog.at_level("INFO"):
        reporter.update()
        soc["v"] = 99.0
        reporter.update()
        ctrl.state, ctrl.kind, ctrl.last_signed_watts = "ACTIVE", "frr", -3500
        reporter.update()
    msgs = [r.message for r in caplog.records if r.message.startswith("reported WORKMODE")]
    assert msgs == [
        "reported WORKMODE normal 0 W SOC 100",
        "reported WORKMODE frrup 3500 W SOC 99",
    ]
    assert dev._workmode_command.to_dict()["BatterySoc"] == 99


def test_bad_payload_does_not_raise():
    dev = ReportingInverterDevice("dev")
    dev.handle_command(b"WORKMODE {not json")
    assert dev._workmode_command.to_dict()["Mode"] == "normal"
