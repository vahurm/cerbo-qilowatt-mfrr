#!/bin/sh
# =============================================================================
# qw_dess_watchdog.sh — failsafe that forces Dynamic ESS back on
# =============================================================================
# Install to /data/qw_dess_watchdog.sh. Run once a minute from a background loop
# (see service/ or /data/rc.local).
#
# If /tmp/qw_dess_off_at is older than QW_MAX_OFF_SECS, force DESS back on via
# qw_dess_toggle.sh. This is the last line of defence: if the orchestration
# layer (Node-RED / the agent) crashes or the network drops mid-event, DESS
# would otherwise stay off forever.
#
# Env overrides:
#   QW_MAX_OFF_SECS   (default 1800)  — max seconds DESS may stay off
#   QW_TOGGLE_SCRIPT  (default /data/qw_dess_toggle.sh)
# =============================================================================

OFF_AT_FILE="/tmp/qw_dess_off_at"
TOGGLE_SCRIPT="${QW_TOGGLE_SCRIPT:-/data/qw_dess_toggle.sh}"
MAX_OFF_SECS="${QW_MAX_OFF_SECS:-1800}"   # 30 minutes; one mFRR event must not exceed this
LOG_TAG="qw_dess_wd"

[ -f "$OFF_AT_FILE" ] || exit 0
[ -x "$TOGGLE_SCRIPT" ] || exit 0

off_at=$(cat "$OFF_AT_FILE" 2>/dev/null)
[ -z "$off_at" ] && exit 0

now=$(date +%s)
age=$((now - off_at))

if [ "$age" -gt "$MAX_OFF_SECS" ]; then
  logger -t "$LOG_TAG" "DESS has been OFF ${age}s (> ${MAX_OFF_SECS}s) — forcing back on (failsafe)"
  "$TOGGLE_SCRIPT" on
fi
