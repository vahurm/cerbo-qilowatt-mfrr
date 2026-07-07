#!/usr/bin/env python3
"""afrr_probe.py — read-only capture + classifier for Qilowatt WORKMODE traffic.

Purpose
-------
Answer, with evidence, the one open question about aFRR: *do aFRR signals reach
this device's WORKMODE stream at all, and if so how do they differ from mFRR?*
It classifies the commands so you can decide whether the custom agent is
UNAFFECTED, SAFE-BUT-IDLE, or RISKY (see docs/AFRR_VERIFICATION.md).

This is a diagnostic. It is strictly READ-ONLY: it never touches dbus, never
runs the /data/qw_*.sh actuators, and in --live mode it only SUBSCRIBEs (it
never publishes and announces no device).

Two safe input sources
-----------------------
* ``--log FILE`` / stdin (default): parse the agent's own log lines of the form
  ``... WORKMODE received: {...}``. Zero network impact — recommended.
  On the Cerbo the log lives at ``/var/log/qw-agent/current`` (multilog).

* ``--live``: passively subscribe to the Qilowatt broker with a DISTINCT client
  id, reusing the creds from ``/data/qw-agent.env``. Use only if you accept a
  second MQTT session for the same account running alongside the live agent.

Examples
--------
    # classify what the running agent has already logged (safe, offline)
    python3 tools/afrr_probe.py --log /var/log/qw-agent/current

    # follow the log live and re-print the verdict as commands arrive
    tail -F /var/log/qw-agent/current | python3 tools/afrr_probe.py

    # passive MQTT sniff (second session; distinct client id, subscribe-only)
    python3 tools/afrr_probe.py --live --capture-file /data/afrr-capture.jsonl
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# What the agent knows about (mirrors mfrr_statemachine + qilowatt-py models)
# --------------------------------------------------------------------------- #

# Sources the state machine treats as an active mFRR event.
MFRR_SOURCES = {"fusebox", "kratt"}
# Non-mFRR sources the agent ignores. Documented by qilowatt-ha (timer,
# optimizer, manual) plus values observed live on the Kungla cloud stream
# ("notimer" = the idle/return-to-normal command after an event).
KNOWN_OTHER_SOURCES = {"timer", "notimer", "optimizer", "manual", "normal", ""}
# Modes documented by qilowatt-ha.
FRR_MODES = {"frrup", "frrdown"}
KNOWN_MODES = {"normal", "buy", "sell", "frrup", "frrdown"}
# WorkModeCommand dataclass fields (qilowatt-py src/qilowatt/models.py). Anything
# outside this set arrives in the library's ``extras`` dict — a strong hint that
# a new signal type (e.g. an aFRR setpoint field) is in play.
KNOWN_WORKMODE_KEYS = {
    "Mode", "_source", "BatterySoc", "PowerLimit", "PeakShaving",
    "ChargeCurrent", "DischargeCurrent", "MaxPower", "MxByPw", "MxSlPw",
}

# Below this median inter-arrival interval (s) a stream of FRR commands looks
# like continuous aFRR-style modulation rather than mFRR block dispatch.
DEFAULT_CONTINUOUS_INTERVAL_S = 30.0

_LOG_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d{3})")


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class Record:
    """One decoded WORKMODE command, normalised for classification."""

    source: str
    mode: str
    power: Optional[int]
    extras: Dict[str, object] = field(default_factory=dict)
    ts: Optional[float] = None
    raw: Dict[str, object] = field(default_factory=dict)

    @property
    def is_frr(self) -> bool:
        return self.source in MFRR_SOURCES and self.mode in FRR_MODES

    @property
    def is_unrecognized(self) -> bool:
        """True if the agent would not recognise this as a known command."""
        unknown_source = self.source not in (MFRR_SOURCES | KNOWN_OTHER_SOURCES)
        unknown_mode = self.mode not in KNOWN_MODES
        return unknown_source or unknown_mode or bool(self.extras)


def _median(values: List[float]) -> float:
    """Median without the stdlib ``statistics`` module (absent on Venus OS)."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _to_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_from_dict(data: dict, ts: Optional[float] = None) -> Record:
    """Normalise a raw WORKMODE dict (as produced by WorkModeCommand.to_dict)."""
    source = str(data.get("_source", "") or "").lower()
    mode = str(data.get("Mode", "normal") or "normal").lower()
    power = _to_int(data.get("PowerLimit"))
    extras = {k: v for k, v in data.items() if k not in KNOWN_WORKMODE_KEYS}
    return Record(source=source, mode=mode, power=power, extras=extras, ts=ts, raw=dict(data))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _parse_log_timestamp(line: str) -> Optional[float]:
    """Extract a POSIX timestamp from the Python logging asctime, if present."""
    m = _LOG_TS_RE.search(line)
    if not m:
        return None
    try:
        base = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        return base.timestamp() + int(m.group(2)) / 1000.0
    except ValueError:
        return None


def parse_log_line(line: str) -> Optional[Record]:
    """Parse an agent log line carrying a ``WORKMODE received: {...}`` payload.

    The agent logs ``command.to_dict()`` via ``%s``, i.e. a Python dict repr
    (single quotes), so we use ``ast.literal_eval`` and fall back to JSON.
    Lines without a WORKMODE payload return None. Any prefix (multilog TAI64N
    label, log level, etc.) is tolerated.
    """
    marker = "WORKMODE received:"
    idx = line.find(marker)
    if idx == -1:
        return None
    payload = line[idx + len(marker):].strip()
    data = _loads_dict(payload)
    if data is None:
        return None
    return record_from_dict(data, ts=_parse_log_timestamp(line))


def parse_mqtt_payload(payload: str) -> Optional[Record]:
    """Parse a raw MQTT command payload (``WORKMODE {json}`` or bare ``{json}``)."""
    text = payload.strip()
    if text.upper().startswith("WORKMODE"):
        text = text[len("WORKMODE"):].strip()
    if text.upper().startswith("BACKLOG"):
        # BACKLOG can chain commands; keep only the first WORKMODE segment.
        seg = text[len("BACKLOG"):]
        wm = seg.upper().find("WORKMODE")
        if wm == -1:
            return None
        text = seg[wm + len("WORKMODE"):].split(";")[0].strip()
    data = _loads_dict(text)
    if data is None:
        return None
    return record_from_dict(data, ts=time.time())


def _loads_dict(text: str) -> Optional[dict]:
    if not text:
        return None
    for loader in (json.loads, ast.literal_eval):
        try:
            obj = loader(text)
        except (ValueError, SyntaxError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

@dataclass
class StreamVerdict:
    total: int = 0
    frr_count: int = 0
    unrecognized_count: int = 0
    sources: Counter = field(default_factory=Counter)
    modes: Counter = field(default_factory=Counter)
    extras_keys: set = field(default_factory=set)
    unknown_sources: set = field(default_factory=set)
    unknown_modes: set = field(default_factory=set)
    frr_median_interval_s: Optional[float] = None
    frr_power_changes: int = 0
    fingerprint: str = "inconclusive"
    decision: str = "INCONCLUSIVE"
    rationale: str = ""


# Map an internal fingerprint to the plan's Step-4 decision buckets.
_DECISION = {
    "continuous_modulation": (
        "RISKY",
        "FRR commands arrive at a continuous (aFRR-like) cadence — the "
        "block-event state machine would toggle DESS on every activation and "
        "thrash. Needs adaptation before relying on aFRR.",
    ),
    "unrecognized_signal": (
        "SAFE-BUT-IDLE",
        "A signal with an unknown source/mode/extra field is arriving; the "
        "agent silently ignores it (no harm, no aFRR revenue). Optional "
        "follow-up to participate.",
    ),
    "block_mfrr": (
        "UNAFFECTED",
        "Only mFRR-style block dispatch was observed; no aFRR fingerprint on "
        "the WORKMODE stream. aFRR is likely handled vendor-side (Modbus R2) "
        "and does not reach this custom agent.",
    ),
    "inconclusive": (
        "INCONCLUSIVE",
        "No FRR or unknown signals in this window. Capture over a longer "
        "period that includes known aFRR activity (cross-check the app).",
    ),
}


def classify_stream(
    records: List[Record],
    continuous_interval_s: float = DEFAULT_CONTINUOUS_INTERVAL_S,
) -> StreamVerdict:
    """Classify a batch of WORKMODE records into an aFRR fingerprint + decision."""
    v = StreamVerdict(total=len(records))
    frr_records: List[Record] = []

    for r in records:
        v.sources[r.source] += 1
        v.modes[r.mode] += 1
        v.extras_keys.update(r.extras.keys())
        if r.source not in (MFRR_SOURCES | KNOWN_OTHER_SOURCES):
            v.unknown_sources.add(r.source)
        if r.mode not in KNOWN_MODES:
            v.unknown_modes.add(r.mode)
        if r.is_unrecognized:
            v.unrecognized_count += 1
        if r.is_frr:
            frr_records.append(r)

    v.frr_count = len(frr_records)

    # Cadence + setpoint churn of the FRR commands (the aFRR-vs-mFRR tell).
    timed = [r.ts for r in frr_records if r.ts is not None]
    timed.sort()
    if len(timed) >= 2:
        intervals = [b - a for a, b in zip(timed, timed[1:]) if b - a >= 0]
        if intervals:
            v.frr_median_interval_s = _median(intervals)
    last_power = None
    for r in frr_records:
        if r.power is not None and r.power != last_power:
            if last_power is not None:
                v.frr_power_changes += 1
            last_power = r.power

    # Fingerprint. Continuous modulation dominates; then unknown signals; then
    # plain block mFRR; else inconclusive.
    continuous = (
        v.frr_median_interval_s is not None
        and v.frr_median_interval_s < continuous_interval_s
        and v.frr_power_changes >= 2
    )
    if continuous:
        v.fingerprint = "continuous_modulation"
    elif v.unrecognized_count > 0:
        v.fingerprint = "unrecognized_signal"
    elif v.frr_count > 0:
        v.fingerprint = "block_mfrr"
    else:
        v.fingerprint = "inconclusive"

    v.decision, v.rationale = _DECISION[v.fingerprint]
    return v


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_summary(v: StreamVerdict) -> str:
    def _top(counter: Counter) -> str:
        if not counter:
            return "(none)"
        return ", ".join(f"{k or '∅'}={n}" for k, n in counter.most_common())

    interval = (
        f"{v.frr_median_interval_s:.1f}s"
        if v.frr_median_interval_s is not None
        else "n/a"
    )
    lines = [
        "=" * 68,
        "aFRR WORKMODE probe — verdict",
        "=" * 68,
        f"  commands seen        : {v.total}",
        f"  sources              : {_top(v.sources)}",
        f"  modes                : {_top(v.modes)}",
        f"  FRR commands         : {v.frr_count}  (median interval {interval}, "
        f"{v.frr_power_changes} power changes)",
        f"  unrecognized commands: {v.unrecognized_count}",
        f"  unknown sources      : {', '.join(sorted(v.unknown_sources)) or '(none)'}",
        f"  unknown modes        : {', '.join(sorted(v.unknown_modes)) or '(none)'}",
        f"  extra WORKMODE keys  : {', '.join(sorted(v.extras_keys)) or '(none)'}",
        "-" * 68,
        f"  FINGERPRINT          : {v.fingerprint}",
        f"  DECISION             : {v.decision}",
        f"  {v.rationale}",
        "=" * 68,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Config loading (shared format with the agent's /data/qw-agent.env)
# --------------------------------------------------------------------------- #

def load_env_file(path: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return env
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


# --------------------------------------------------------------------------- #
# Input drivers
# --------------------------------------------------------------------------- #

def run_from_log(
    stream, continuous_interval_s: float, follow: bool, summary_every: int
) -> int:
    """Read log lines from ``stream``; classify and print a verdict."""
    records: List[Record] = []
    since_summary = 0
    for line in stream:
        rec = parse_log_line(line)
        if rec is None:
            continue
        records.append(rec)
        since_summary += 1
        _print_command(rec)
        if follow and summary_every and since_summary >= summary_every:
            since_summary = 0
            print(render_summary(classify_stream(records, continuous_interval_s)))
    print(render_summary(classify_stream(records, continuous_interval_s)))
    return 0


def run_live(
    env: Dict[str, str],
    continuous_interval_s: float,
    capture_file: Optional[str],
    summary_every: int,
) -> int:
    """Passively subscribe to the Qilowatt broker (subscribe-only, distinct id)."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt is required for --live (pip install paho-mqtt)", file=sys.stderr)
        return 2

    device_id = env.get("QW_DEVICE_ID", "")
    user = env.get("QW_MQTT_USER", "")
    password = env.get("QW_MQTT_PASS", "")
    host = env.get("QW_MQTT_HOST", "mqtt.qilowatt.it")
    port = int(env.get("QW_MQTT_PORT", "8883"))
    use_tls = env.get("QW_MQTT_TLS", "1") == "1"
    if not (device_id and user and password):
        print(
            "Missing QW_DEVICE_ID / QW_MQTT_USER / QW_MQTT_PASS in the env file.",
            file=sys.stderr,
        )
        return 2

    topic = f"Q/{device_id}/#"
    records: List[Record] = []
    state = {"since_summary": 0}
    cap_fh = open(capture_file, "a", encoding="utf-8") if capture_file else None

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"afrr-probe-{os.getpid()}"
    )
    client.username_pw_set(user, password)
    if use_tls:
        client.tls_set()

    def on_connect(cl, _ud, _flags, reason, _props=None):
        print(f"[live] connected ({reason}); subscribing to {topic}")
        cl.subscribe(topic, qos=0)

    def on_message(_cl, _ud, msg):
        # Only command topics carry WORKMODE dispatch; ignore telemetry.
        if not msg.topic.endswith(("cmnd/backlog", "cmnd/Backlog", "WORKMODE")):
            return
        rec = parse_mqtt_payload(msg.payload.decode("utf-8", "replace"))
        if rec is None:
            return
        records.append(rec)
        _print_command(rec, topic=msg.topic)
        if cap_fh is not None:
            cap_fh.write(json.dumps({"ts": rec.ts, "topic": msg.topic, "raw": rec.raw}) + "\n")
            cap_fh.flush()
        state["since_summary"] += 1
        if summary_every and state["since_summary"] >= summary_every:
            state["since_summary"] = 0
            print(render_summary(classify_stream(records, continuous_interval_s)))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    print("[live] subscribe-only probe running; Ctrl-C to stop and print verdict.")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[live] stopping…")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        if cap_fh is not None:
            cap_fh.close()
    print(render_summary(classify_stream(records, continuous_interval_s)))
    return 0


def _print_command(rec: Record, topic: Optional[str] = None) -> None:
    when = (
        datetime.fromtimestamp(rec.ts).strftime("%H:%M:%S")
        if rec.ts is not None
        else "--:--:--"
    )
    tag = "FRR" if rec.is_frr else ("UNREC" if rec.is_unrecognized else "other")
    extra = f" extras={rec.extras}" if rec.extras else ""
    loc = f" [{topic}]" if topic else ""
    print(
        f"{when} {tag:5} src={rec.source or '∅'} mode={rec.mode} "
        f"power={rec.power}{extra}{loc}"
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read-only capture + classifier for Qilowatt WORKMODE / aFRR.",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--log",
        metavar="FILE",
        help="parse a saved/streaming agent log (default: read stdin)",
    )
    src.add_argument(
        "--live",
        action="store_true",
        help="passively subscribe to the Qilowatt broker (subscribe-only)",
    )
    p.add_argument(
        "--env",
        default=os.environ.get("QW_AGENT_ENV", "/data/qw-agent.env"),
        help="env file with QW_* creds for --live (default: %(default)s)",
    )
    p.add_argument(
        "--capture-file",
        help="in --live mode, append each raw command as JSONL to this file",
    )
    p.add_argument(
        "--continuous-interval",
        type=float,
        default=DEFAULT_CONTINUOUS_INTERVAL_S,
        help="median FRR interval (s) below which the stream looks continuous "
        "(aFRR-like) rather than mFRR block dispatch (default: %(default)s)",
    )
    p.add_argument(
        "--summary-every",
        type=int,
        default=20,
        help="re-print the rolling verdict every N commands while following "
        "(0 disables; default: %(default)s)",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.live:
        env = load_env_file(args.env)
        return run_live(
            env, args.continuous_interval, args.capture_file, args.summary_every
        )

    if args.log:
        with open(args.log, "r", encoding="utf-8", errors="replace") as fh:
            return run_from_log(
                fh, args.continuous_interval, follow=True, summary_every=args.summary_every
            )

    # Default: read stdin (supports `tail -F ... | afrr_probe.py`).
    return run_from_log(
        sys.stdin, args.continuous_interval, follow=True, summary_every=args.summary_every
    )


if __name__ == "__main__":
    sys.exit(main())
