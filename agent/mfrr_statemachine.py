"""Headless mFRR state machine — the Python equivalent of the Node-RED flow.

Driven by:
  * WORKMODE commands from the Qilowatt cloud (via qw_agent.py),
  * the QW connection state (connected / disconnected),
  * a periodic `tick()` so the failsafes fire even when the broker is quiet.

It drives the actuators (DESS toggle + grid setpoint). This lets a Cerbo run
Qilowatt mFRR with no Node-RED and no Home Assistant.

State diagram:

    IDLE  --(_source in fusebox/kratt)-->  ACTIVE
      ^                                       |
      |  setpoint 0 + DESS on                 |  DESS off, then signed setpoint
      +---------------------------------------+
         (_source normal | mqtt_lost>5min | event>30min)

Sign convention: frrup (export) -> negative setpoint; frrdown (import) ->
positive setpoint. PowerLimit is always reported as a positive magnitude.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, Optional

from qilowatt import WorkModeCommand

_logger = logging.getLogger("qw_agent.mfrr")

DEFAULT_MFRR_SOURCES = ("fusebox", "kratt")


class MfrrController:
    def __init__(
        self,
        actuator,
        mfrr_sources: Iterable[str] = DEFAULT_MFRR_SOURCES,
        mqtt_lost_failsafe_s: float = 300.0,
        max_duration_s: float = 1800.0,
        dess_off_delay_s: float = 2.0,
    ) -> None:
        self._act = actuator
        self._sources = tuple(s.strip().lower() for s in mfrr_sources)
        self._mqtt_lost_failsafe_s = mqtt_lost_failsafe_s
        self._max_duration_s = max_duration_s
        self._dess_off_delay_s = dess_off_delay_s

        self._lock = threading.RLock()
        self._state = "IDLE"
        self._event_start: Optional[float] = None
        self._last_signed_watts = 0
        self._connected = True
        self._disconnected_at: Optional[float] = None
        self._pending_timer: Optional[threading.Timer] = None
        # Token guards the delayed setpoint against a race with event end.
        self._token = 0

    # ------------------------------------------------------------------ #
    # Inputs
    # ------------------------------------------------------------------ #
    def on_workmode(self, command: WorkModeCommand) -> None:
        data = command.to_dict()
        source = str(data.get("_source", "") or "").lower()
        mode = str(data.get("Mode", "normal") or "normal").lower()
        try:
            power = int(data.get("PowerLimit", 0) or 0)
        except (TypeError, ValueError):
            power = 0

        is_mfrr = source in self._sources
        signed = -abs(power) if mode == "frrup" else abs(power)

        with self._lock:
            self._apply(is_mfrr, signed)

    def on_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected
            if connected:
                self._disconnected_at = None
            elif self._disconnected_at is None:
                self._disconnected_at = time.monotonic()

    def tick(self) -> None:
        """Periodic failsafe check (call ~every 10 s)."""
        with self._lock:
            if self._state != "ACTIVE":
                return
            now = time.monotonic()
            if (
                not self._connected
                and self._disconnected_at is not None
                and now - self._disconnected_at > self._mqtt_lost_failsafe_s
            ):
                _logger.warning(
                    "FAILSAFE: QW link lost > %ss while ACTIVE -> revert",
                    self._mqtt_lost_failsafe_s,
                )
                self._revert()
                return
            if (
                self._event_start is not None
                and now - self._event_start > self._max_duration_s
            ):
                _logger.warning(
                    "FAILSAFE: mFRR event > %ss -> revert", self._max_duration_s
                )
                self._revert()

    def shutdown(self) -> None:
        """On clean stop, revert an active event so the system is left safe."""
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
            if self._state == "ACTIVE":
                _logger.info("shutdown during ACTIVE event -> revert to safe")
                self._revert()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    # ------------------------------------------------------------------ #
    # Transitions (call with the lock held)
    # ------------------------------------------------------------------ #
    def _apply(self, is_mfrr: bool, signed: int) -> None:
        if self._state == "IDLE" and is_mfrr:
            self._enter_active(signed)
        elif self._state == "ACTIVE" and is_mfrr:
            if signed != self._last_signed_watts:
                self._last_signed_watts = signed
                _logger.info("mFRR setpoint update: %s W", signed)
                self._act.set_setpoint(signed)
        elif self._state == "ACTIVE" and not is_mfrr:
            self._revert()

    def _enter_active(self, signed: int) -> None:
        self._state = "ACTIVE"
        self._event_start = time.monotonic()
        self._last_signed_watts = signed
        self._token += 1
        token = self._token
        _logger.info(
            "mFRR START: DESS off, then %s W after %ss", signed, self._dess_off_delay_s
        )
        self._act.dess_off()
        # Apply the setpoint after the DESS-off settle delay, guarded by token.
        timer = threading.Timer(
            self._dess_off_delay_s, self._apply_delayed_setpoint, args=(token,)
        )
        timer.daemon = True
        self._pending_timer = timer
        timer.start()

    def _apply_delayed_setpoint(self, token: int) -> None:
        with self._lock:
            if self._state != "ACTIVE" or token != self._token:
                return
            self._pending_timer = None
            self._act.set_setpoint(self._last_signed_watts)

    def _revert(self) -> None:
        self._state = "IDLE"
        self._event_start = None
        self._last_signed_watts = 0
        self._disconnected_at = None
        self._token += 1  # invalidate any pending delayed setpoint
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
        _logger.info("mFRR END: grid setpoint 0, DESS on")
        # Release the setpoint before restoring DESS so they don't fight.
        self._act.set_setpoint(0)
        self._act.dess_on()
