"""Headless mFRR state machine — the Python equivalent of the Node-RED flow.

Driven by:
  * WORKMODE commands from the Qilowatt cloud (via qw_agent.py),
  * the QW connection state (connected / disconnected),
  * a periodic `tick()` so the failsafes fire even when the broker is quiet.

It drives the actuators (DESS toggle + grid setpoint). This lets a Cerbo run
Qilowatt mFRR with no Node-RED and no Home Assistant.

State diagram:

    IDLE  --(_source in mfrr_sources AND Mode in frrup/frrdown AND power != 0)--> ACTIVE
      ^                                                                            |
      |  setpoint 0 + DESS on                        DESS off, then signed setpoint
      +----------------------------------------------------------------------------+
         (non-FRR source or Mode | zero-power FRR | mqtt_lost>5min | event>max)

All three gates matter.

The *source* gate keeps strangers out. The *mode* gate matters because a single
`_source` speaks several dialects: the `qilowatt` source sends `frrup`/`frrdown`
balancing dispatch *and* `buy` optimiser trades. Without it a `buy` would fall
through the `-abs() if frrup else abs()` sign rule and be actuated as a
full-power grid import with DESS off.

The *power* gate exists because a zero-power `frrup`/`frrdown` is the dispatcher's
routine stand-down, not an instruction to hold 0 W. Held as an event it would
park DESS off and the SOC floor lowered while delivering nothing, so it ends the
event instead. Measured on Kungla (2026-07-03..27): 121 zero-power FRR commands
in 24 days, 52 of which stranded the site — 4.4 h of pointless DESS-off in
total, median 4.8 min, worst 12.5 min — every one rescued only because some
unrelated later command happened to arrive and end the event.

Sign convention: frrup (export) -> negative setpoint; frrdown (import) ->
positive setpoint. PowerLimit is always reported as a positive magnitude.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional

from qilowatt import WorkModeCommand

_logger = logging.getLogger("qw_agent.mfrr")

DEFAULT_MFRR_SOURCES = ("fusebox", "kratt")

# The only Modes that carry a balancing setpoint. Deliberately not
# env-configurable: it is the hard guard that keeps a non-FRR Mode from a
# trusted source (e.g. `qilowatt` + `buy`) out of the actuators.
FRR_MODES = ("frrup", "frrdown")


class MfrrController:
    def __init__(
        self,
        actuator,
        mfrr_sources: Iterable[str] = DEFAULT_MFRR_SOURCES,
        frr_modes: Iterable[str] = FRR_MODES,
        mqtt_lost_failsafe_s: float = 300.0,
        max_duration_s: float = 1800.0,
        dess_off_delay_s: float = 2.0,
        on_state_change: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._act = actuator
        self._sources = tuple(s.strip().lower() for s in mfrr_sources)
        self._modes = tuple(m.strip().lower() for m in frr_modes)
        self._mqtt_lost_failsafe_s = mqtt_lost_failsafe_s
        self._max_duration_s = max_duration_s
        self._dess_off_delay_s = dess_off_delay_s
        # Optional hook fired (state, signed_watts) on every ACTIVE/IDLE change.
        # Used to bridge the mFRR state to a local broker so the Node-RED
        # curtailment flow can stand down while mFRR owns the grid setpoint.
        # Set as a public attribute so it can be wired after construction.
        self.on_state_change = on_state_change

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

        is_frr = source in self._sources and mode in self._modes
        # Zero power on an FRR Mode is a stand-down, not a 0 W dispatch to hold.
        is_mfrr = is_frr and power != 0
        signed = -abs(power) if mode == "frrup" else abs(power)

        if is_frr and not is_mfrr:
            _logger.info(
                "%s Mode %r carries 0 W -> stand-down, not a 0 W dispatch",
                source,
                mode,
            )
        elif source in self._sources and not is_frr:
            _logger.info(
                "ignoring non-FRR Mode %r from mFRR source %r (PowerLimit=%s)",
                mode,
                source,
                power,
            )

        with self._lock:
            self._apply(is_mfrr, signed, "%s/%s %s W" % (source or "?", mode, power))

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
                self._revert("failsafe: QW link lost > %ss" % self._mqtt_lost_failsafe_s)
                return
            if (
                self._event_start is not None
                and now - self._event_start > self._max_duration_s
            ):
                _logger.warning(
                    "FAILSAFE: mFRR event > %ss -> revert", self._max_duration_s
                )
                self._revert("failsafe: event > %ss" % self._max_duration_s)

    def shutdown(self) -> None:
        """On clean stop, revert an active event so the system is left safe."""
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
            if self._state == "ACTIVE":
                _logger.info("shutdown during ACTIVE event -> revert to safe")
                self._revert("agent shutdown")

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def last_signed_watts(self) -> int:
        """Current signed mFRR setpoint (neg=frrup/export, pos=frrdown/import)."""
        with self._lock:
            return self._last_signed_watts

    def _notify(self) -> None:
        """Fire the state-change hook; never let a listener break the machine."""
        cb = self.on_state_change
        if cb is None:
            return
        try:
            cb(self._state, self._last_signed_watts)
        except Exception as exc:  # pragma: no cover - defensive
            _logger.error("on_state_change callback error: %s", exc)

    # ------------------------------------------------------------------ #
    # Transitions (call with the lock held)
    # ------------------------------------------------------------------ #
    def _apply(self, is_mfrr: bool, signed: int, reason: str = "") -> None:
        if self._state == "IDLE" and is_mfrr:
            self._enter_active(signed)
        elif self._state == "ACTIVE" and is_mfrr:
            if signed != self._last_signed_watts:
                self._last_signed_watts = signed
                _logger.info("mFRR setpoint update: %s W", signed)
                self._act.set_setpoint(signed)
                self._notify()
        elif self._state == "ACTIVE" and not is_mfrr:
            self._revert(reason)

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
        self._notify()

    def _apply_delayed_setpoint(self, token: int) -> None:
        with self._lock:
            if self._state != "ACTIVE" or token != self._token:
                return
            self._pending_timer = None
            self._act.set_setpoint(self._last_signed_watts)

    def _revert(self, reason: str = "") -> None:
        self._state = "IDLE"
        self._event_start = None
        self._last_signed_watts = 0
        self._disconnected_at = None
        self._token += 1  # invalidate any pending delayed setpoint
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
        # Name the trigger: without it an END line cannot be told apart from a
        # failsafe, a stand-down or a foreign automation writing the same topic,
        # and attributing one means hand-matching timestamps across two logs.
        _logger.info(
            "mFRR END (%s): grid setpoint 0, DESS on", reason or "reason unrecorded"
        )
        # Release the setpoint before restoring DESS so they don't fight.
        self._act.set_setpoint(0)
        self._act.dess_on()
        self._notify()
