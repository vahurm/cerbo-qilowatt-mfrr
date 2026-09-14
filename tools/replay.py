#!/usr/bin/env python3
"""replay.py — run a captured WORKMODE log through the state machine, offline.

Answers "what WOULD the agent have done on my site?" before going live, and
doubles as a regression test against real data:

    python3 tools/replay.py --log /data/afrr-workmode.log
    python3 tools/replay.py --log agent.log --env /data/qw-agent.env --timeline

Input is any file with the agent's ``WORKMODE received: {...}`` lines (the
durable capture ``/data/afrr-workmode.log`` or ``/var/log/qw-agent/current``).
Commands are fed to ``MfrrController`` with a recording actuator and a clock
driven by the log timestamps; the failsafe ``tick()`` runs at the configured
interval between commands, so duration caps fire exactly as they would live.
No dbus, no network, nothing actuated.

Configuration is read like the agent does — from ``--env`` (a qw-agent.env
file) and/or the process environment (QW_MFRR_SOURCES, QW_TRADE_MODES,
QW_MAX_EVENT_S, QW_MAX_TRADE_S, QW_MAX_IMPORT_W, QW_MAX_EXPORT_W, ...). The
SOC-target end of a trade cannot be replayed (no live SOC); trades end on the
next command or the cap, which is the pessimistic case.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "agent"), os.path.join(_HERE, "..", "pylib"), _HERE):
    _p = os.path.abspath(_p)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import mfrr_statemachine  # noqa: E402
from afrr_probe import load_env_file, parse_log_line  # noqa: E402
from mfrr_statemachine import MfrrController  # noqa: E402


# --------------------------------------------------------------------------- #
# Deterministic clock + timers
# --------------------------------------------------------------------------- #

class _Clock:
    def __init__(self, start: float) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


class _Timer:
    """threading.Timer stand-in fired by the replay loop when its time comes."""

    pending: List["_Timer"] = []

    def __init__(self, interval, function, args=None, kwargs=None) -> None:
        self.due: Optional[float] = None
        self.interval = float(interval)
        self.function = function
        self.args = tuple(args or ())
        self.kwargs = dict(kwargs or {})
        self.cancelled = False
        self.daemon = False

    def start(self) -> None:
        self.due = _Timer.now + self.interval
        _Timer.pending.append(self)

    def cancel(self) -> None:
        self.cancelled = True

    now: float = 0.0

    @classmethod
    def fire_due(cls, now: float) -> None:
        cls.now = now
        due = [t for t in cls.pending if not t.cancelled and t.due is not None and t.due <= now]
        cls.pending = [t for t in cls.pending if t not in due and not t.cancelled]
        for t in due:
            t.function(*t.args, **t.kwargs)


class _Cmd:
    def __init__(self, data: dict) -> None:
        self._d = data

    def to_dict(self) -> dict:
        return dict(self._d)


# --------------------------------------------------------------------------- #
# Recording actuator + event timeline
# --------------------------------------------------------------------------- #

@dataclass
class Event:
    start: float
    kind: str
    signed_w: int
    end: Optional[float] = None
    reason: str = ""
    peak_w: int = 0
    setpoints: List[int] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.end or self.start) - self.start


class _RecordingActuator:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.calls: List[tuple] = []
        self.dess_off_since: Optional[float] = None
        self.dess_off_total_s = 0.0

    def dess_off(self, lower_floor: bool = True) -> bool:
        self.calls.append((self._clock(), "dess_off", lower_floor))
        if self.dess_off_since is None:
            self.dess_off_since = self._clock()
        return True

    def dess_on(self) -> bool:
        self.calls.append((self._clock(), "dess_on"))
        if self.dess_off_since is not None:
            self.dess_off_total_s += self._clock() - self.dess_off_since
            self.dess_off_since = None
        return True

    def set_setpoint(self, watts: int) -> bool:
        self.calls.append((self._clock(), "set_setpoint", int(watts)))
        return True


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass
class ReplayResult:
    commands: int = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    events: List[Event] = field(default_factory=list)
    dess_off_total_s: float = 0.0
    capped: int = 0
    failsafes: int = 0
    dropped_unlisted: int = 0
    ignored_non_frr: int = 0
    standdowns: int = 0

    @property
    def frr_events(self) -> List[Event]:
        return [e for e in self.events if e.kind == "frr"]

    @property
    def trade_events(self) -> List[Event]:
        return [e for e in self.events if e.kind == "trade"]


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #

def _env_tuple(env: dict, key: str, default: str) -> tuple:
    return tuple(s.strip().lower() for s in env.get(key, default).split(",") if s.strip())


def _env_float(env: dict, key: str, default: Optional[float]) -> Optional[float]:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def controller_from_env(env: dict, actuator, on_state_change=None) -> MfrrController:
    return MfrrController(
        actuator,
        mfrr_sources=_env_tuple(env, "QW_MFRR_SOURCES", ",".join(mfrr_statemachine.DEFAULT_MFRR_SOURCES)),
        mqtt_lost_failsafe_s=_env_float(env, "QW_MQTT_LOST_FAILSAFE_S", 300.0),
        max_duration_s=_env_float(env, "QW_MAX_EVENT_S", 7200.0),
        dess_off_delay_s=_env_float(env, "QW_DESS_OFF_DELAY_S", 2.0),
        trade_sources=_env_tuple(env, "QW_TRADE_SOURCES", "qilowatt"),
        trade_modes=_env_tuple(env, "QW_TRADE_MODES", "buy,sell"),
        max_trade_s=_env_float(env, "QW_MAX_TRADE_S", 5400.0),
        max_import_w=_env_float(env, "QW_MAX_IMPORT_W", None),
        max_export_w=_env_float(env, "QW_MAX_EXPORT_W", None),
        on_state_change=on_state_change,
    )


def replay(lines, env: dict, tick_s: float = 10.0) -> ReplayResult:
    """Feed parsed WORKMODE lines through a fresh controller; return the result."""
    records = [r for r in (parse_log_line(ln) for ln in lines) if r is not None and r.ts is not None]
    records.sort(key=lambda r: r.ts)
    result = ReplayResult(commands=len(records))
    if not records:
        return result
    result.first_ts, result.last_ts = records[0].ts, records[-1].ts

    clock = _Clock(records[0].ts)
    _Timer.pending = []
    _Timer.now = clock.value
    orig_monotonic, orig_timer = mfrr_statemachine.time.monotonic, mfrr_statemachine.threading.Timer
    mfrr_statemachine.time.monotonic = clock  # type: ignore[assignment]
    mfrr_statemachine.threading.Timer = _Timer  # type: ignore[assignment]

    capture = _LogCapture()
    logger = logging.getLogger("qw_agent.mfrr")
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    prev_propagate = logger.propagate
    logger.propagate = False

    actuator = _RecordingActuator(clock)
    current: List[Event] = []

    def on_state(state: str, signed: int) -> None:
        if state == "ACTIVE":
            if current and current[0].kind != (ctrl.kind or ""):
                # kind switch in place: close the old, open the new
                current[0].end = clock(); current[0].reason = "switched to %s" % ctrl.kind
                result.events.append(current.pop())
            if not current:
                current.append(Event(start=clock(), kind=ctrl.kind or "?", signed_w=signed))
            ev = current[0]
            ev.setpoints.append(signed)
            ev.peak_w = max(ev.peak_w, abs(signed))
        elif current:
            ev = current.pop()
            ev.end = clock()
            # the END line carries the reason
            for rec in reversed(capture.records):
                if " END (" in rec.getMessage():
                    msg = rec.getMessage()
                    ev.reason = msg[msg.index("(") + 1: msg.rindex(")")]
                    break
            result.events.append(ev)

    ctrl = controller_from_env(env, actuator, on_state_change=on_state)

    try:
        for rec in records:
            # advance in tick-sized steps so failsafes and the settle timer fire on time
            while clock.value + tick_s < rec.ts:
                clock.value += tick_s
                _Timer.fire_due(clock.value)
                ctrl.tick()
            clock.value = rec.ts
            _Timer.fire_due(clock.value)
            ctrl.tick()
            ctrl.on_workmode(_Cmd(rec.raw))
            _Timer.fire_due(clock.value)
        # let a still-open event run into its cap so DESS-off hours are honest
        deadline = clock.value + max(_env_float(env, "QW_MAX_EVENT_S", 7200.0), _env_float(env, "QW_MAX_TRADE_S", 5400.0)) + tick_s
        while ctrl.state == "ACTIVE" and clock.value < deadline:
            clock.value += tick_s
            _Timer.fire_due(clock.value)
            ctrl.tick()
        if ctrl.state == "ACTIVE":
            ctrl.shutdown()
    finally:
        mfrr_statemachine.time.monotonic = orig_monotonic  # type: ignore[assignment]
        mfrr_statemachine.threading.Timer = orig_timer  # type: ignore[assignment]
        logger.removeHandler(capture)
        logger.propagate = prev_propagate

    if actuator.dess_off_since is not None:
        actuator.dess_off_total_s += clock.value - actuator.dess_off_since
    result.dess_off_total_s = actuator.dess_off_total_s
    for r in capture.records:
        m = r.getMessage()
        if m.startswith("capping "):
            result.capped += 1
        elif m.startswith("FAILSAFE"):
            result.failsafes += 1
        elif m.startswith("dropping FRR dispatch"):
            result.dropped_unlisted += 1
        elif m.startswith("ignoring non-FRR Mode"):
            result.ignored_non_frr += 1
        elif "stand-down" in m:
            result.standdowns += 1
    return result


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _ts(t: Optional[float]) -> str:
    return "-" if t is None else datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def _hms(s: float) -> str:
    s = int(round(s))
    return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def render(result: ReplayResult, env: dict, timeline: bool = False) -> str:
    out: List[str] = []
    out.append("replay: %d WORKMODE commands, %s .. %s (%s)" % (
        result.commands, _ts(result.first_ts), _ts(result.last_ts),
        _hms((result.last_ts or 0) - (result.first_ts or 0)),
    ))
    out.append("config: sources=%s trades=%s caps import=%s export=%s W, max event=%s s, max trade=%s s" % (
        env.get("QW_MFRR_SOURCES", ",".join(mfrr_statemachine.DEFAULT_MFRR_SOURCES)),
        env.get("QW_TRADE_MODES", "buy,sell") or "-",
        env.get("QW_MAX_IMPORT_W", "-"), env.get("QW_MAX_EXPORT_W", "-"),
        env.get("QW_MAX_EVENT_S", "7200"), env.get("QW_MAX_TRADE_S", "5400"),
    ))
    frr, trades = result.frr_events, result.trade_events
    out.append("")
    out.append("events the agent WOULD have run:")
    for label, evs in (("mFRR", frr), ("trade", trades)):
        if not evs:
            out.append("  %-6s none" % label)
            continue
        durs = sorted(e.duration_s for e in evs)
        med = durs[len(durs) // 2]
        out.append("  %-6s %3d  median %s  longest %s  peak %d W  total %s" % (
            label, len(evs), _hms(med), _hms(durs[-1]), max(e.peak_w for e in evs),
            _hms(sum(durs)),
        ))
    out.append("  DESS off in total: %s" % _hms(result.dess_off_total_s))
    out.append("")
    out.append("gates and failsafes:")
    out.append("  stand-downs (0 W event Mode)        %d" % result.standdowns)
    out.append("  capped to QW_MAX_IMPORT/EXPORT_W    %d%s" % (
        result.capped, "" if result.capped == 0 else "   <- the market asks more than your caps deliver"))
    out.append("  FAILSAFE truncations                %d%s" % (
        result.failsafes, "" if result.failsafes == 0 else "   <- raise QW_MAX_EVENT_S / QW_MAX_TRADE_S or check the link"))
    out.append("  FRR dispatch from unlisted source   %d%s" % (
        result.dropped_unlisted, "" if result.dropped_unlisted == 0 else "   <- add it to QW_MFRR_SOURCES"))
    out.append("  non-FRR Modes ignored               %d" % result.ignored_non_frr)
    ends = {}
    for e in result.events:
        is_standdown = e.reason.endswith(" 0 W") and any(
            "/%s 0 W" % m in e.reason for m in ("frrup", "frrdown", "buy", "sell")
        )
        key = "failsafe" if e.reason.startswith("failsafe") else (
            "stand-down" if is_standdown else (
                "switched" if e.reason.startswith("switched") else (
                    "shutdown" if "shutdown" in e.reason else "other command")))
        ends[key] = ends.get(key, 0) + 1
    if ends:
        out.append("  how events ended: " + ", ".join("%s=%d" % kv for kv in sorted(ends.items())))
    if timeline and result.events:
        out.append("")
        out.append("timeline:")
        for e in sorted(result.events, key=lambda e: e.start):
            out.append("  %s  %-5s %+7d W  %s  end: %s" % (
                _ts(e.start), e.kind, e.signed_w, _hms(e.duration_s), e.reason or "-"))
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--log", required=True, help="agent log or afrr-workmode.log with WORKMODE lines")
    p.add_argument("--env", help="qw-agent.env to take QW_* settings from (process env overrides)")
    p.add_argument("--tick", type=float, default=10.0, help="failsafe tick interval, s (default 10)")
    p.add_argument("--timeline", action="store_true", help="print every event")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    env = {}
    if args.env:
        env.update(load_env_file(args.env))
    env.update({k: v for k, v in os.environ.items() if k.startswith("QW_")})
    with open(args.log, "r", encoding="utf-8", errors="replace") as fh:
        result = replay(fh, env, tick_s=args.tick)
    print(render(result, env, timeline=args.timeline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
