"""mFRR actuators — drive the Cerbo via the /data/qw_*.sh scripts (or dry-run).

The state machine calls a small interface (dess_off / dess_on / set_setpoint).
Each call returns ``True`` when the write was confirmed and ``False`` when it
was rejected, failed, or reads back a different value. `ScriptActuator` shells
out to the shared shell scripts and then READS BACK the register it just wrote:
the setpoint clamp rejects out-of-range values (exit 3) and leaves the previous
setpoint in place, and a dbus hiccup can swallow a write silently — in both
cases the state machine used to believe the event was being delivered.
`DryRunActuator` only logs — used for validation before cutover, where the
agent runs without touching dbus.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import List, Optional, Tuple

_logger = logging.getLogger("qw_agent.actuators")

_SETPOINT_GET_RE = re.compile(r"AcPowerSetPoint\s*=\s*(-?[0-9]+(?:\.[0-9]+)?)")
# qw_grid_setpoint.sh re-reads the register after writing and prints
# "AcPowerSetPoint: <old> -> <new> W"; <new> IS the read-back.
_SETPOINT_WRITE_RE = re.compile(r"AcPowerSetPoint:\s*-?[0-9.]+\s*->\s*(-?[0-9]+(?:\.[0-9]+)?)\s*W")
_DESS_MODE_RE = re.compile(r"DESS Mode \(live\)\s*=\s*(-?[0-9]+(?:\.[0-9]+)?)")
# qw_dess_toggle.sh off/on re-read the Mode and print "..., live now <mode>".
_DESS_LIVE_RE = re.compile(r"live now (-?[0-9]+(?:\.[0-9]+)?)")


class ScriptActuator:
    """Drives the Cerbo by executing the /data/qw_*.sh actuator scripts."""

    def __init__(
        self,
        dess_script: str = "/data/qw_dess_toggle.sh",
        setpoint_script: str = "/data/qw_grid_setpoint.sh",
        timeout_s: float = 10.0,
        verify: bool = True,
    ) -> None:
        self._dess = dess_script
        self._setpoint = setpoint_script
        self._timeout = timeout_s
        self._verify = verify

    def _run(self, cmd: List[str]) -> Tuple[bool, str]:
        """Run a script; return (exit-ok, stdout). Never raises."""
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
                return False, out
            _logger.info("actuator %s -> %s", cmd, out)
            return True, out
        except Exception as exc:
            _logger.error("actuator %s failed: %s", cmd, exc)
            return False, ""

    # --- read-backs --------------------------------------------------------- #
    def read_setpoint(self) -> Optional[float]:
        """Live AcPowerSetPoint via ``qw_grid_setpoint.sh get``; None if unknown."""
        ok, out = self._run([self._setpoint, "get"])
        m = _SETPOINT_GET_RE.search(out) if ok else None
        return float(m.group(1)) if m else None

    def read_dess_mode(self) -> Optional[float]:
        """Live DESS Mode via ``qw_dess_toggle.sh status``; None if unknown."""
        ok, out = self._run([self._dess, "status"])
        m = _DESS_MODE_RE.search(out) if ok else None
        return float(m.group(1)) if m else None

    @staticmethod
    def _first_float(regex, text: str) -> Optional[float]:
        m = regex.search(text or "")
        return float(m.group(1)) if m else None

    def _verify_dess(self, out: str, want_off: bool, what: str) -> bool:
        """Confirm the DESS Mode from the script's own output, else `status`.

        Each script call costs ~2 s on a Cerbo (the dbus CLI is slow), so the
        write's echoed live value is preferred over a second invocation.
        """
        mode = self._first_float(_DESS_LIVE_RE, out)
        if mode is None:
            mode = self.read_dess_mode()
        if mode is None:
            _logger.warning("DESS Mode could not be read back after '%s'; assuming ok", what)
            return True
        if want_off and mode != 0:
            _logger.error("actuation failed: DESS Mode reads %s after 'off'", mode)
            return False
        if not want_off and mode == 0:
            _logger.error("actuation failed: DESS Mode still 0 after 'on'")
            return False
        return True

    # --- actuation ---------------------------------------------------------- #
    def dess_off(self, lower_floor: bool = True) -> bool:
        """Turn DESS off; ``lower_floor=False`` leaves the SOC floor alone.

        FRR dispatch lowers the shared ESS floor to QW_MFRR_MIN_SOC so frrup
        can discharge below the arbitrage floor. A Q trade must not: a buy
        charges upward anyway and a sell has to respect the owner's floor.
        """
        cmd = [self._dess, "off"]
        if not lower_floor:
            cmd.append("--no-floor")
        ok, out = self._run(cmd)
        if not ok:
            return False
        if not self._verify:
            return True
        return self._verify_dess(out, want_off=True, what="off")

    def dess_on(self) -> bool:
        ok, out = self._run([self._dess, "on"])
        if not ok:
            return False
        if not self._verify:
            return True
        return self._verify_dess(out, want_off=False, what="on")

    def set_setpoint(self, watts: int) -> bool:
        target = int(watts)
        ok, out = self._run([self._setpoint, str(target)])
        if not ok:
            return False
        if not self._verify:
            return True
        live = self._first_float(_SETPOINT_WRITE_RE, out)
        if live is None:
            live = self.read_setpoint()
        if live is None:
            _logger.warning("setpoint could not be read back after write; assuming ok")
            return True
        if abs(live - target) > 1.0:
            _logger.error(
                "actuation failed: setpoint reads %s W after writing %s W", live, target
            )
            return False
        return True


class DryRunActuator:
    """Logs intended actions without touching the system (validation phase)."""

    def dess_off(self, lower_floor: bool = True) -> bool:
        _logger.info("[dry-run] DESS off%s", "" if lower_floor else " (--no-floor)")
        return True

    def dess_on(self) -> bool:
        _logger.info("[dry-run] DESS on")
        return True

    def set_setpoint(self, watts: int) -> bool:
        _logger.info("[dry-run] grid setpoint %s W", int(watts))
        return True
