"""Tests for the read-only aFRR probe/classifier (tools/afrr_probe.py).

These lock down the parsing of agent log lines and the aFRR fingerprinting so
the verify-only workflow gives a trustworthy verdict. The tool never touches
dbus or actuators, so there is nothing to mock here — it is pure functions.
"""

from __future__ import annotations

import afrr_probe as ap


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_parse_log_line_decodes_dict_repr_and_timestamp():
    line = (
        "@400000006870... 2026-07-07 23:10:00,123 INFO qw_agent: "
        "WORKMODE received: {'Mode': 'frrdown', '_source': 'kratt', 'PowerLimit': 3000}"
    )
    rec = ap.parse_log_line(line)
    assert rec is not None
    assert rec.source == "kratt"
    assert rec.mode == "frrdown"
    assert rec.power == 3000
    assert rec.ts is not None
    assert rec.is_frr


def test_parse_log_line_ignores_non_workmode_lines():
    assert ap.parse_log_line("2026-07-07 23:10:00,000 INFO qw_agent: QW connected") is None
    assert ap.parse_log_line("random noise") is None


def test_parse_mqtt_payload_workmode_prefix_json():
    rec = ap.parse_mqtt_payload('WORKMODE {"Mode":"frrup","_source":"kratt","PowerLimit":5000}')
    assert rec is not None
    assert rec.mode == "frrup"
    assert rec.power == 5000
    assert rec.is_frr


def test_extras_captured_for_unknown_keys():
    rec = ap.record_from_dict(
        {"Mode": "frrup", "_source": "kratt", "PowerLimit": 100, "Setpoint": 42}
    )
    assert rec.extras == {"Setpoint": 42}
    assert rec.is_unrecognized  # unknown key -> agent would drop the extra


# --------------------------------------------------------------------------- #
# Classification / fingerprints
# --------------------------------------------------------------------------- #

def _frr(ts, power, source="kratt", mode="frrdown"):
    return ap.Record(source=source, mode=mode, power=power, ts=ts)


def test_block_mfrr_stream_is_unaffected():
    # Sparse block dispatch: ~5 min apart, same magnitude held per event.
    recs = [_frr(0, 3000), _frr(300, 3000), _frr(600, 4000)]
    v = ap.classify_stream(recs)
    assert v.fingerprint == "block_mfrr"
    assert v.decision == "UNAFFECTED"


def test_continuous_modulation_stream_is_risky():
    # aFRR-like: a few seconds apart with constantly changing setpoints.
    recs = [_frr(t, 1000 + 100 * i) for i, t in enumerate(range(0, 60, 5))]
    v = ap.classify_stream(recs, continuous_interval_s=30.0)
    assert v.fingerprint == "continuous_modulation"
    assert v.decision == "RISKY"
    assert v.frr_median_interval_s is not None and v.frr_median_interval_s <= 5


def test_unrecognized_source_is_safe_but_idle():
    recs = [ap.Record(source="afrr", mode="regulate", power=500, ts=0.0)]
    v = ap.classify_stream(recs)
    assert v.fingerprint == "unrecognized_signal"
    assert v.decision == "SAFE-BUT-IDLE"
    assert "afrr" in v.unknown_sources
    assert "regulate" in v.unknown_modes


def test_unknown_extra_key_flags_safe_but_idle():
    recs = [
        ap.record_from_dict(
            {"Mode": "frrdown", "_source": "kratt", "PowerLimit": 3000, "AfrrSetpoint": 12},
            ts=0.0,
        ),
        ap.record_from_dict(
            {"Mode": "frrdown", "_source": "kratt", "PowerLimit": 3000, "AfrrSetpoint": 34},
            ts=300.0,
        ),
    ]
    v = ap.classify_stream(recs)
    assert "AfrrSetpoint" in v.extras_keys
    # Sparse cadence, but the unknown key makes it a candidate aFRR signal.
    assert v.fingerprint == "unrecognized_signal"
    assert v.decision == "SAFE-BUT-IDLE"


def test_empty_or_normal_only_is_inconclusive():
    recs = [ap.Record(source="optimizer", mode="normal", power=0, ts=0.0)]
    v = ap.classify_stream(recs)
    assert v.fingerprint == "inconclusive"
    assert v.decision == "INCONCLUSIVE"


def test_render_summary_contains_decision():
    v = ap.classify_stream([_frr(0, 3000), _frr(300, 3000)])
    out = ap.render_summary(v)
    assert "DECISION" in out
    assert "UNAFFECTED" in out
