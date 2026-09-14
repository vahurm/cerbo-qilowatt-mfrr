"""Start-up routines for qw_agent: leftover-event recovery and config sanity.

Both run once in ``main()`` before the Qilowatt link is opened, so a fresh
process never inherits a half-finished event and a misconfigured site is told
so in its own log at the moment it matters.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

_logger = logging.getLogger("qw_agent.startup")

# Must match scripts/qw_dess_toggle.sh / scripts/qw_dess_watchdog.sh.
DEFAULT_STATE_DIR = "/data"
SAVED_MODE_FILE = "qw_dess_saved_mode"
SAVED_MINSOC_FILE = "qw_dess_saved_minsoc"
DEFAULT_OFF_AT_FILE = "/tmp/qw_dess_off_at"
# qw_dess_watchdog.sh's QW_MAX_OFF_SECS default (pinned by a test).
DEFAULT_WATCHDOG_MAX_OFF_S = 7800.0
# qw_grid_setpoint.sh's built-in per-direction limits (pinned by a test).
SCRIPT_DEFAULT_LIMIT_W = 15000.0

# Live ESS SOC floor (the one qw_dess_toggle.sh lowers for FRR).
SVC_SETTINGS = "com.victronenergy.settings"
PATH_MIN_SOC = "/Settings/CGwacs/BatteryLife/MinimumSocLimit"


def leftover_event(state_dir: str = DEFAULT_STATE_DIR, off_at_file: str = DEFAULT_OFF_AT_FILE) -> Optional[str]:
    """Describe a DESS-off left behind by a previous process, or None.

    A clean stop reverts the event (``MfrrController.shutdown``) and the
    toggle script removes its state files on ``on``. Only a crash, ``kill -9``
    or a reboot leaves them: the saved-Mode file lives on /data and survives a
    reboot, the off-at stamp on /tmp does not — which is exactly the case the
    watchdog cannot see (it keys on the /tmp stamp), so DESS would stay off
    for good and the grid setpoint would stay parked.
    """
    parts: List[str] = []
    saved_mode = os.path.join(state_dir, SAVED_MODE_FILE)
    saved_floor = os.path.join(state_dir, SAVED_MINSOC_FILE)
    if os.path.isfile(saved_mode):
        parts.append("saved DESS Mode %s" % _read(saved_mode))
    if os.path.isfile(saved_floor):
        parts.append("saved SOC floor %s%%" % _read(saved_floor))
    if os.path.isfile(off_at_file):
        try:
            age = int(time.time() - float(_read(off_at_file)))
            parts.append("DESS off for %ds" % age)
        except ValueError:
            parts.append("DESS off-at stamp present")
    return ", ".join(parts) if parts else None


def recover_leftover_event(
    actuator,
    state_dir: str = DEFAULT_STATE_DIR,
    off_at_file: str = DEFAULT_OFF_AT_FILE,
) -> bool:
    """Return the site to normal if a previous run died mid-event.

    Safe default: setpoint 0 and DESS back on. If the event is in fact still
    running, the post-connect WORKMODE snapshot (~20 s later) reopens it.
    Returns True when a recovery was performed.
    """
    found = leftover_event(state_dir, off_at_file)
    if not found:
        return False
    _logger.warning(
        "STARTUP RECOVERY: previous run left an event open (%s) -> setpoint 0, DESS on",
        found,
    )
    ok_sp = actuator.set_setpoint(0) is not False
    ok_dess = actuator.dess_on() is not False
    if ok_sp and ok_dess:
        _logger.info("STARTUP RECOVERY done")
    else:
        _logger.error(
            "STARTUP RECOVERY incomplete: setpoint=%s dess_on=%s (actuation failed)",
            ok_sp, ok_dess,
        )
    return True


def config_warnings(
    cfg,
    reader=None,
    watchdog_max_off_s: Optional[float] = None,
    env=None,
) -> List[str]:
    """Return human-readable warnings about a risky/unfinished configuration.

    Each is logged by the caller as ``CONFIG WARN: ...`` (an audit pattern).
    None of these stop the agent — they are the checks that, until now, only
    the test-suite and the documents enforced.
    """
    env = os.environ if env is None else env
    warns: List[str] = []

    if watchdog_max_off_s is None:
        raw = env.get("QW_MAX_OFF_SECS", "").strip()
        try:
            watchdog_max_off_s = float(raw) if raw else DEFAULT_WATCHDOG_MAX_OFF_S
        except ValueError:
            watchdog_max_off_s = DEFAULT_WATCHDOG_MAX_OFF_S

    for name, value in (("QW_MAX_EVENT_S", cfg.max_event_s), ("QW_MAX_TRADE_S", cfg.max_trade_s)):
        if value >= watchdog_max_off_s:
            warns.append(
                "%s=%d is not below the DESS watchdog cap QW_MAX_OFF_SECS=%d: the "
                "watchdog would restore DESS while the agent still holds the grid "
                "setpoint" % (name, value, watchdog_max_off_s)
            )

    if cfg.max_import_w is None or cfg.max_export_w is None:
        warns.append(
            "QW_MAX_IMPORT_W/QW_MAX_EXPORT_W not set: the agent does not cap "
            "requests, only qw_grid_setpoint.sh's built-in %d W limit applies (it "
            "REJECTS instead of capping) — set them to your grid connection"
            % SCRIPT_DEFAULT_LIMIT_W
        )
    elif cfg.max_import_w == SCRIPT_DEFAULT_LIMIT_W and cfg.max_export_w == SCRIPT_DEFAULT_LIMIT_W:
        warns.append(
            "QW_MAX_IMPORT_W and QW_MAX_EXPORT_W are both at the example value "
            "%d W — confirm they match your grid connection" % SCRIPT_DEFAULT_LIMIT_W
        )

    min_soc_raw = env.get("QW_MFRR_MIN_SOC", "").strip()
    if min_soc_raw:
        try:
            min_soc = float(min_soc_raw)
        except ValueError:
            warns.append("QW_MFRR_MIN_SOC=%r is not a number; the floor will not be lowered" % min_soc_raw)
        else:
            live = None
            if reader is not None and getattr(reader, "available", False):
                live = reader.get(SVC_SETTINGS, PATH_MIN_SOC, None)
            if live is not None:
                try:
                    live_f = float(live)
                except (TypeError, ValueError):
                    live_f = None
                if live_f is not None and min_soc >= live_f:
                    warns.append(
                        "QW_MFRR_MIN_SOC=%g is not below the live ESS SOC floor %g%%: "
                        "frrup cannot discharge below what DESS already allows"
                        % (min_soc, live_f)
                    )

    if not cfg.trade_modes and cfg.trade_sources:
        warns.append(
            "QW_TRADE_MODES is empty: Qilowatt 'buy' SOC-preparation trades are "
            "dropped and the battery will sit at the mFRR floor between activations"
        )

    return warns


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "?"
