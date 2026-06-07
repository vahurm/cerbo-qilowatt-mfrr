"""mFRR actuators — drive the Cerbo via the /data/qw_*.sh scripts (or dry-run).

The state machine calls a small interface (dess_off / dess_on / set_setpoint).
`ScriptActuator` shells out to the shared shell scripts (same ones the HA and
Node-RED solutions use). `DryRunActuator` only logs — used for the parallel /
validation phase before cutover, where the agent runs without touching dbus.
"""

from __future__ import annotations

import logging
import subprocess
from typing import List

_logger = logging.getLogger("qw_agent.actuators")


class ScriptActuator:
    """Drives the Cerbo by executing the /data/qw_*.sh actuator scripts."""

    def __init__(
        self,
        dess_script: str = "/data/qw_dess_toggle.sh",
        setpoint_script: str = "/data/qw_grid_setpoint.sh",
        timeout_s: float = 10.0,
    ) -> None:
        self._dess = dess_script
        self._setpoint = setpoint_script
        self._timeout = timeout_s

    def _run(self, cmd: List[str]) -> None:
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self._timeout
            )
            out = (res.stdout or "").strip()
            err = (res.stderr or "").strip()
            if res.returncode != 0:
                _logger.error(
                    "actuator %s exit %s: %s", cmd, res.returncode, err or out
                )
            else:
                _logger.info("actuator %s -> %s", cmd, out)
        except Exception as exc:
            _logger.error("actuator %s failed: %s", cmd, exc)

    def dess_off(self) -> None:
        self._run([self._dess, "off"])

    def dess_on(self) -> None:
        self._run([self._dess, "on"])

    def set_setpoint(self, watts: int) -> None:
        self._run([self._setpoint, str(int(watts))])


class DryRunActuator:
    """Logs intended actions without touching the system (validation phase)."""

    def dess_off(self) -> None:
        _logger.info("[dry-run] DESS off")

    def dess_on(self) -> None:
        _logger.info("[dry-run] DESS on")

    def set_setpoint(self, watts: int) -> None:
        _logger.info("[dry-run] grid setpoint %s W", int(watts))
