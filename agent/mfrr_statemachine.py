"""Headless mFRR state machine — the Python equivalent of the Node-RED flow.

Driven by:
  * WORKMODE commands from the Qilowatt cloud (via qw_agent.py),
  * the QW connection state (connected / disconnected),
  * a periodic `tick()` so the failsafes fire even when the broker is quiet.

It drives the actuators (DESS toggle + grid setpoint). This lets a Cerbo run
Qilowatt mFRR with no Node-RED and no Home Assistant.

State diagram (one ACTIVE state, two event kinds):

    IDLE  --(FRR dispatch: source in mfrr_sources, Mode frrup/frrdown, power != 0)--> ACTIVE[frr]
    IDLE  --(Q trade:      source in trade_sources, Mode buy/sell,     power != 0)--> ACTIVE[trade]
      ^                                                                                |
      |  setpoint 0 + DESS on                                DESS off, then signed setpoint
      +--------------------------------------------------------------------------------+
         (non-event source/Mode | zero-power event Mode | mqtt_lost>5min | event>max |
          trade: live SOC reached the BatterySoc target)

    ACTIVE[frr] <--> ACTIVE[trade]: a command of the other kind switches kind and
    rewrites the setpoint WITHOUT cycling DESS on/off in between (DESS is already
    off; cycling it would let arbitrage grab the inverter for a few seconds).

The *source* gate keeps strangers out. The *mode* gate matters because a single
`_source` speaks several dialects: the `qilowatt` source sends `frrup`/`frrdown`
balancing dispatch *and* `buy` SOC-preparation trades. Only the configured
`trade_modes` from the configured `trade_sources` are actuated as trades;
anything else from a trusted source is dropped rather than falling through the
`-abs() if frrup else abs()` sign rule as a full-power grid import.

Why trades are actuated at all: measured on site A (2026-07-14..09-14) the
`qilowatt`/`buy` command (PowerLimit 20-27 kW, BatterySoc 100) arrives 5-30 min
after a `kratt`/`frrup` stand-down and is followed 5-30 min later by the next
`frrup` dispatch — the vendor refills the battery between activations so the
next up-regulation can be delivered (portal banner: "Qilowatt (Q) charges the
battery with additional energy when needed", frrup paid at 1400 EUR/MWh). With
the trade dropped the battery sits at the mFRR floor and the next activation is
short or never dispatched. Trades were dropped 22 times in one ~10-day log ring.

The *power* gate exists because a zero-power `frrup`/`frrdown` is the dispatcher's
routine stand-down, not an instruction to hold 0 W. Held as an event it would
park DESS off and the SOC floor lowered while delivering nothing, so it ends the
event instead. Measured on site A (2026-07-03..27): 121 zero-power FRR commands
in 24 days, 52 of which stranded the site — 4.4 h of pointless DESS-off in
total, median 4.8 min, worst 12.5 min — every one rescued only because some
unrelated later command happened to arrive and end the event. The same rule
applies to a zero-power `buy`/`sell`.

Sign convention: frrup / sell (export) -> negative setpoint; frrdown / buy
(import) -> positive setpoint. PowerLimit is always reported as a positive
magnitude. When `max_import_w` / `max_export_w` are given the magnitude is
capped here as well as in the setpoint script: the script REJECTS an
out-of-range value and leaves the previous setpoint in place, whereas capping
delivers what the connection allows (site A's `buy` asks 27 kW against a 28 kW
import cap; a 15 kW export cap would otherwise reject a 27 kW `sell` outright).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable, Optional

from qilowatt import WorkModeCommand

_logger = logging.getLogger("qw_agent.mfrr")

# `_source` values whose frrup/frrdown is balancing dispatch. `qilowatt` is
# included: the vendor dispatches some sites directly (site B, 2026-07) and a
# default that drops those would silently deliver nothing. The Mode gate below
# still keeps `qilowatt`'s non-FRR Modes out of the actuators.
DEFAULT_MFRR_SOURCES = ("fusebox", "kratt", "qilowatt")

# The only Modes that carry a balancing setpoint. Deliberately not
# env-configurable: it is the hard guard that keeps a non-FRR Mode from a
# trusted source (e.g. `qilowatt` + `savebattery`) out of the actuators.
FRR_MODES = ("frrup", "frrdown")

# The only Modes that may be actuated as an SOC-preparation trade. `buy` imports
# (positive setpoint), `sell` exports (negative). Which of these a deployment
# actually honours is configured per site (QW_TRADE_MODES); the set itself is
# fixed so `limitexport`/`savebattery`/`normal` can never be actuated.
TRADE_MODES = ("buy", "sell")
# Trades are only honoured from the vendor's own SOC optimiser by default. The
# market dispatchers (`kratt`, `fusebox`) never send buy/sell.
DEFAULT_TRADE_SOURCES = ("qilowatt",)

KIND_FRR = "frr"
KIND_TRADE = "trade"

# Returns the live battery SOC in percent, or None when it cannot be read.
SocReader = Callable[[], Optional[float]]


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
        trade_sources: Iterable[str] = DEFAULT_TRADE_SOURCES,
        trade_modes: Iterable[str] = (),
        max_trade_s: float = 5400.0,
        max_import_w: Optional[float] = None,
        max_export_w: Optional[float] = None,
        soc_reader: Optional[SocReader] = None,
    ) -> None:
        self._act = actuator
        self._sources = tuple(s.strip().lower() for s in mfrr_sources)
        self._modes = tuple(m.strip().lower() for m in frr_modes)
        self._trade_sources = tuple(s.strip().lower() for s in trade_sources)
        # Only the fixed TRADE_MODES can ever be enabled, whatever the config says.
        self._trade_modes = tuple(
            m.strip().lower() for m in trade_modes if m.strip().lower() in TRADE_MODES
        )
        self._mqtt_lost_failsafe_s = mqtt_lost_failsafe_s
        self._max_duration_s = max_duration_s
        self._max_trade_s = max_trade_s
        self._dess_off_delay_s = dess_off_delay_s
        self._max_import_w = None if max_import_w is None else abs(float(max_import_w))
        self._max_export_w = None if max_export_w is None else abs(float(max_export_w))
        self._soc_reader = soc_reader
        # Optional hook fired (state, signed_watts) on every ACTIVE/IDLE change.
        # Used to bridge the mFRR state to a local broker so the Node-RED
        # curtailment flow can stand down while mFRR owns the grid setpoint.
        # Set as a public attribute so it can be wired after construction.
        self.on_state_change = on_state_change

        self._lock = threading.RLock()
        self._state = "IDLE"
        self._kind: Optional[str] = None
        self._event_start: Optional[float] = None
        self._last_signed_watts = 0
        self._soc_target: Optional[float] = None
        self._connected = True
        self._disconnected_at: Optional[float] = None
        self._pending_timer: Optional[threading.Timer] = None
        # Token guards the delayed setpoint against a race with event end.
        self._token = 0
        # True once an actuator write failed its read-back even after a retry:
        # the machine still tracks the event (so the end command is honoured)
        # but the site is NOT delivering what it claims. Cleared on the next
        # confirmed write. Surfaced via `degraded` + the on_state_change hook.
        self._degraded = False

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
        is_trade = (
            not is_frr
            and source in self._trade_sources
            and mode in self._trade_modes
        )
        is_event_mode = is_frr or is_trade
        # Zero power on an event Mode is a stand-down, not a 0 W dispatch to hold.
        is_event = is_event_mode and power != 0

        kind = KIND_FRR if is_frr else (KIND_TRADE if is_trade else None)
        export = mode in ("frrup", "sell")
        signed = -self._cap(power, export) if export else self._cap(power, export)

        soc_target: Optional[float] = None
        if is_trade:
            soc_target = _to_float(data.get("BatterySoc"))

        if is_event_mode and not is_event:
            _logger.info(
                "%s Mode %r carries 0 W -> stand-down, not a 0 W dispatch",
                source,
                mode,
            )
        elif mode in FRR_MODES and power != 0 and source not in self._sources:
            # A real dispatch from a source the operator did not list. Loud on
            # purpose: on a site dispatched by a source missing from
            # QW_MFRR_SOURCES every activation would otherwise vanish silently.
            _logger.warning(
                "dropping FRR dispatch from unlisted source %r (Mode %r, %s W) — "
                "add it to QW_MFRR_SOURCES if it is your dispatcher",
                source, mode, power,
            )
        elif (source in self._sources or source in self._trade_sources) and not is_event_mode:
            _logger.info(
                "ignoring non-FRR Mode %r from mFRR source %r (PowerLimit=%s)",
                mode,
                source,
                power,
            )

        with self._lock:
            self._apply(
                is_event,
                kind,
                signed,
                soc_target,
                "%s/%s %s W" % (source or "?", mode, power),
            )

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
            cap = self._max_trade_s if self._kind == KIND_TRADE else self._max_duration_s
            if self._event_start is not None and now - self._event_start > cap:
                _logger.warning(
                    "FAILSAFE: %s event > %ss -> revert", self._kind, cap
                )
                self._revert("failsafe: event > %ss" % cap)
                return
            if self._kind == KIND_TRADE:
                self._check_trade_target()

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
    def kind(self) -> Optional[str]:
        """``"frr"`` / ``"trade"`` while ACTIVE, ``None`` while IDLE."""
        with self._lock:
            return self._kind

    @property
    def last_signed_watts(self) -> int:
        """Current signed setpoint (neg=export, pos=import)."""
        with self._lock:
            return self._last_signed_watts

    @property
    def degraded(self) -> bool:
        """True while the last actuator write could not be confirmed."""
        with self._lock:
            return self._degraded

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _actuate(self, what: str, fn: Callable[..., object], *args, **kwargs) -> bool:
        """Call an actuator method, retry once on a failed read-back.

        Actuators return ``True``/``False``; ``None`` (legacy/fake actuators
        without read-back) counts as success. A second failure logs
        ``actuation failed`` (the audit pattern) and raises the degraded flag;
        the event state itself is unchanged so the eventual end command, the
        duration cap and the DESS watchdog still get to clean up.
        """
        ok = fn(*args, **kwargs) is not False
        if not ok:
            _logger.warning("actuator %s not confirmed; retrying once", what)
            ok = fn(*args, **kwargs) is not False
        if not ok:
            if not self._degraded:
                self._degraded = True
                _logger.error("actuation failed: %s (after retry) -> DEGRADED", what)
                self._notify()
            else:
                _logger.error("actuation failed: %s (after retry)", what)
        elif self._degraded:
            self._degraded = False
            _logger.info("actuator %s confirmed -> degraded cleared", what)
            self._notify()
        return ok

    def _cap(self, power: int, export: bool) -> int:
        limit = self._max_export_w if export else self._max_import_w
        magnitude = abs(power)
        if limit is not None and magnitude > limit:
            _logger.info(
                "capping %s %s W to the %s limit %s W",
                "export" if export else "import",
                magnitude,
                "export" if export else "import",
                int(limit),
            )
            return int(limit)
        return magnitude

    def _check_trade_target(self) -> None:
        """End a trade once the live SOC has reached the commanded BatterySoc.

        A read failure never ends the event: the next command or the duration
        cap will. Ending on a bogus reading would be worse than running on.
        """
        if self._soc_target is None or self._soc_reader is None:
            return
        try:
            soc = self._soc_reader()
        except Exception as exc:
            _logger.warning("SOC read failed during trade: %s", exc)
            return
        if soc is None:
            return
        importing = self._last_signed_watts > 0
        if importing and soc >= self._soc_target:
            self._revert("trade target reached: SOC %.0f%% >= %.0f%%" % (soc, self._soc_target))
        elif not importing and soc <= self._soc_target:
            self._revert("trade target reached: SOC %.0f%% <= %.0f%%" % (soc, self._soc_target))

    def _notify(self) -> None:
        """Fire the state-change hook; never let a listener break the machine."""
        cb = self.on_state_change
        if cb is None:
            return
        try:
            cb(self._state, self._last_signed_watts)
        except Exception as exc:  # pragma: no cover - defensive
            _logger.error("on_state_change callback error: %s", exc)

    @staticmethod
    def _label(kind: Optional[str]) -> str:
        return "TRADE" if kind == KIND_TRADE else "mFRR"

    # ------------------------------------------------------------------ #
    # Transitions (call with the lock held)
    # ------------------------------------------------------------------ #
    def _apply(
        self,
        is_event: bool,
        kind: Optional[str],
        signed: int,
        soc_target: Optional[float],
        reason: str = "",
    ) -> None:
        if self._state == "IDLE" and is_event:
            self._enter_active(kind, signed, soc_target)
        elif self._state == "ACTIVE" and is_event:
            if kind != self._kind:
                # DESS is already off: switch kind in place, restart the
                # duration clock for the new event, rewrite the setpoint.
                _logger.info(
                    "%s -> %s switch (%s): setpoint %s W, DESS stays off",
                    self._label(self._kind), self._label(kind), reason, signed,
                )
                self._kind = kind
                self._event_start = time.monotonic()
                self._soc_target = soc_target
                if kind == KIND_FRR:
                    # The trade opened DESS-off without lowering the SOC floor;
                    # dispatch needs it. `off` is idempotent on the saved Mode.
                    self._actuate("DESS off (floor)", self._act.dess_off, lower_floor=True)
                if self._pending_timer is None:
                    self._last_signed_watts = signed
                    self._actuate("setpoint %s W" % signed, self._act.set_setpoint, signed)
                else:
                    # Settle timer still pending: let it write the new value.
                    self._last_signed_watts = signed
                self._notify()
                return
            self._soc_target = soc_target if kind == KIND_TRADE else None
            if signed != self._last_signed_watts:
                self._last_signed_watts = signed
                _logger.info("%s setpoint update: %s W", self._label(kind), signed)
                if self._pending_timer is None:
                    self._actuate("setpoint %s W" % signed, self._act.set_setpoint, signed)
                self._notify()
        elif self._state == "ACTIVE" and not is_event:
            self._revert(reason)

    def _enter_active(self, kind: str, signed: int, soc_target: Optional[float]) -> None:
        self._state = "ACTIVE"
        self._kind = kind
        self._event_start = time.monotonic()
        self._last_signed_watts = signed
        self._soc_target = soc_target
        self._token += 1
        token = self._token
        target = (
            " (until SOC %.0f%%)" % soc_target
            if kind == KIND_TRADE and soc_target is not None
            else ""
        )
        _logger.info(
            "%s START: DESS off, then %s W after %ss%s",
            self._label(kind), signed, self._dess_off_delay_s, target,
        )
        # Only FRR dispatch lowers the shared SOC floor; a trade leaves it alone.
        self._actuate(
            "DESS off", self._act.dess_off, lower_floor=(kind == KIND_FRR)
        )
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
            self._actuate(
                "setpoint %s W" % self._last_signed_watts,
                self._act.set_setpoint,
                self._last_signed_watts,
            )

    def _revert(self, reason: str = "") -> None:
        label = self._label(self._kind)
        self._state = "IDLE"
        self._kind = None
        self._event_start = None
        self._last_signed_watts = 0
        self._soc_target = None
        self._disconnected_at = None
        self._token += 1  # invalidate any pending delayed setpoint
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
        # Name the trigger: without it an END line cannot be told apart from a
        # failsafe, a stand-down or a foreign automation writing the same topic,
        # and attributing one means hand-matching timestamps across two logs.
        _logger.info(
            "%s END (%s): grid setpoint 0, DESS on", label, reason or "reason unrecorded"
        )
        # Release the setpoint before restoring DESS so they don't fight.
        self._actuate("setpoint 0 W", self._act.set_setpoint, 0)
        self._actuate("DESS on", self._act.dess_on)
        self._notify()


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
