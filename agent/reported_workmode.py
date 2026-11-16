"""WORKMODE object echoed in SENSOR: the applied state, not the raw command.

qilowatt-py stores the last inbound ``WORKMODE`` and puts it back in every
``SENSOR`` payload. The portal device chip reads that object (``Mode``,
``PowerLimit``, ``BatterySoc``) and treats it as the inverter's own mode —
which is what a Deye reports. On Victron the library was echoing the command
unchanged, so a Kratt stand-down (``frrup``, ``PowerLimit`` 0, ``BatterySoc``
= the discharge floor) stayed on the chip after the agent had already
released the setpoint and restored DESS. The flow diagram, which reads
``METRICS`` / ``ENERGY``, kept showing the real charge and power.

This module builds the object the chip should show, and a device subclass
that does not store the raw command before the agent has decided.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from qilowatt import InverterDevice, WorkModeCommand

_logger = logging.getLogger("qw_agent.workmode")

SocReader = Callable[[], Optional[float]]


def reported_workmode(
    state: str,
    kind: Optional[str],
    signed_watts: int,
    soc: Optional[float],
) -> dict:
    """Applied mode for the portal chip.

    ``BatterySoc`` is the measured charge. It is omitted when the reading is
    missing — the command's floor or trade target must never be shown as the
    battery percentage. ``PowerLimit`` is the magnitude the agent is actually
    holding. Anything that is not an active non-zero event is ``normal`` / 0 W,
    including a zero-power stand-down the state machine has already released.
    """
    mode = "normal"
    power = 0
    try:
        signed = int(signed_watts)
    except (TypeError, ValueError):
        signed = 0
    if state == "ACTIVE" and signed != 0:
        power = abs(signed)
        if kind == "frr":
            mode = "frrup" if signed < 0 else "frrdown"
        elif kind == "trade":
            mode = "sell" if signed < 0 else "buy"
        else:
            mode = "normal"
            power = 0
    payload = {"Mode": mode, "PowerLimit": power}
    if soc is not None:
        try:
            payload["BatterySoc"] = int(round(float(soc)))
        except (TypeError, ValueError):
            pass
    return payload


class ReportingInverterDevice(InverterDevice):
    """InverterDevice whose SENSOR WORKMODE is whatever the agent stored.

    The upstream ``handle_command`` writes the raw command into the echo
    *before* the callback returns. A stand-down would then be published for
    the whole DESS-off script. This override only parses and forwards; the
    echo stays on the previous applied mode until ``set_reported_workmode``.
    """

    def set_reported_workmode(self, command: WorkModeCommand) -> None:
        self._workmode_command = command

    def handle_command(self, payload: bytes) -> None:
        try:
            message = payload.decode("utf-8")
            if message.startswith("WORKMODE"):
                data = json.loads(message[len("WORKMODE "):])
                command = WorkModeCommand.from_dict(data)
                if self._on_command_callback:
                    self._on_command_callback(command)
        except Exception as exc:
            _logger.error("Error processing command message: %s", exc)


class WorkmodeReporter:
    """Keep the device echo aligned with the state machine and the live SOC.

    Mode and power are logged only when they change. SOC is refreshed on
    every call (the telemetry loop) without a log line, so a long discharge
    does not leave the chip on the SOC from the start of the event.
    """

    def __init__(self, device: ReportingInverterDevice, controller, soc_reader: SocReader) -> None:
        self._device = device
        self._controller = controller
        self._soc_reader = soc_reader
        self._last_key = None

    def update(self) -> None:
        soc = None
        try:
            soc = self._soc_reader()
        except Exception as exc:
            _logger.warning("SOC read failed while reporting WORKMODE: %s", exc)
        payload = reported_workmode(
            self._controller.state,
            self._controller.kind,
            self._controller.last_signed_watts,
            soc,
        )
        self._device.set_reported_workmode(WorkModeCommand.from_dict(payload))
        key = (payload["Mode"], payload["PowerLimit"])
        if key == self._last_key:
            return
        self._last_key = key
        if "BatterySoc" in payload:
            soc_bit = "SOC %s" % payload["BatterySoc"]
        else:
            soc_bit = "SOC unknown"
        _logger.info(
            "reported WORKMODE %s %s W %s",
            payload["Mode"], payload["PowerLimit"], soc_bit,
        )
