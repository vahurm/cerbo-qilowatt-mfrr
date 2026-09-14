"""Tests for tools/replay.py — offline replay of a WORKMODE log."""

from __future__ import annotations

import replay

LINES = [
    "2026-09-10 10:00:00,000 INFO qw_agent: WORKMODE received: {'_source': 'kratt', 'Mode': 'frrup', 'PowerLimit': 12000, 'BatterySoc': 10}",
    "2026-09-10 10:12:00,000 INFO qw_agent: WORKMODE received: {'_source': 'kratt', 'Mode': 'frrup', 'PowerLimit': 0, 'BatterySoc': 10}",
    "2026-09-10 10:20:00,000 INFO qw_agent: WORKMODE received: {'_source': 'qilowatt', 'Mode': 'buy', 'PowerLimit': 27000, 'BatterySoc': 100}",
    "2026-09-10 10:50:00,000 INFO qw_agent: WORKMODE received: {'_source': 'kratt', 'Mode': 'frrup', 'PowerLimit': 20000, 'BatterySoc': 10}",
    "2026-09-10 13:30:00,000 INFO qw_agent: WORKMODE received: {'_source': 'optimizer', 'Mode': 'normal', 'PowerLimit': 0}",
    "2026-09-10 14:00:00,000 INFO qw_agent: WORKMODE received: {'_source': 'fusebox', 'Mode': 'frrdown', 'PowerLimit': 5000}",
    "2026-09-10 14:05:00,000 INFO qw_agent: WORKMODE received: {'_source': 'notimer', 'Mode': 'normal', 'PowerLimit': 0}",
    "2026-09-10 15:00:00,000 INFO qw_agent: WORKMODE received: {'_source': 'elering', 'Mode': 'frrup', 'PowerLimit': 3000}",
    "garbage line without a payload",
]

ENV = {"QW_MAX_IMPORT_W": "28000", "QW_MAX_EXPORT_W": "15000"}


def test_replay_event_timeline():
    r = replay.replay(LINES, ENV)
    assert r.commands == 8
    kinds = [(e.kind, e.signed_w) for e in sorted(r.events, key=lambda e: e.start)]
    assert kinds == [("frr", -12000), ("trade", 27000), ("frr", -15000), ("frr", 5000)]

    first, trade, capped, last = sorted(r.events, key=lambda e: e.start)
    assert first.duration_s == 12 * 60 and first.reason == "kratt/frrup 0 W"
    assert trade.reason == "switched to frr"
    # 20 kW export capped to 15 kW; ran into the 7200 s cap (next tick after it)
    assert capped.peak_w == 15000 and capped.reason.startswith("failsafe: event > 7200")
    assert 7200 <= capped.duration_s <= 7220
    assert last.reason == "notimer/normal 0 W"


def test_replay_counters():
    r = replay.replay(LINES, ENV)
    assert r.standdowns == 1
    assert r.capped == 1
    assert r.failsafes == 1
    assert r.dropped_unlisted == 1          # elering/frrup
    assert r.ignored_non_frr == 0           # optimizer is not a trusted source
    # DESS off: 12 min + (10:20 -> failsafe ~12:50:10) + 5 min
    assert abs(r.dess_off_total_s - (12 * 60 + 2 * 3600 + 30 * 60 + 10 + 5 * 60)) <= 20


def test_replay_respects_env_gates():
    r = replay.replay(LINES, {"QW_TRADE_MODES": "", "QW_MFRR_SOURCES": "fusebox"})
    assert [e.kind for e in r.events] == ["frr"]          # only the fusebox frrdown
    assert r.dropped_unlisted == 3                          # kratt x2 (non-zero), elering
    assert r.ignored_non_frr == 1                           # qilowatt/buy with trades off


def test_replay_restores_monkeypatched_clock():
    import time
    import threading

    import mfrr_statemachine

    replay.replay(LINES, ENV)
    assert mfrr_statemachine.time.monotonic is time.monotonic
    assert mfrr_statemachine.threading.Timer is threading.Timer


def test_replay_empty_log():
    r = replay.replay(["nothing here"], ENV)
    assert r.commands == 0 and r.events == []
    assert "0 WORKMODE commands" in replay.render(r, ENV)


def test_render_mentions_hints(capsys):
    r = replay.replay(LINES, ENV)
    text = replay.render(r, ENV, timeline=True)
    assert "add it to QW_MFRR_SOURCES" in text
    assert "raise QW_MAX_EVENT_S" in text
    assert "timeline:" in text and "switched to frr" in text
    assert "how events ended: failsafe=1, other command=1, stand-down=1, switched=1" in text
