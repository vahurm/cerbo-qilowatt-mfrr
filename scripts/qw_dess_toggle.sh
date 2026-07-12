#!/bin/sh
# =============================================================================
# qw_dess_toggle.sh — prepare / restore the system for an mFRR event
# =============================================================================
# Install to /data/qw_dess_toggle.sh (persists across Venus OS firmware updates).
# Permissions: chmod 750
#
# For the duration of an mFRR event this does TWO things atomically:
#   1. save + turn Dynamic ESS OFF (Mode=0) so QW owns the grid setpoint, and
#   2. optionally LOWER the ESS minimum-SOC floor to QW_MFRR_MIN_SOC so mFRR can
#      discharge deeper than the normal DESS arbitrage floor — then restore it.
#
# WHY the floor move (important): on this Venus build the Dynamic ESS delegate
# (dbus-systemcalc-py/delegates/dynamicess.py) reads its floor from the SINGLE
# ESS minimum SOC (CGwacs/BatteryLife/MinimumSocLimit -> /Control/ActiveSocLimit),
# NOT from /Settings/DynamicEss/MinSoc. So arbitrage (DESS on) and mFRR (DESS off)
# share ONE floor register. To let arbitrage rest at X while mFRR reaches Y (Y<X),
# the agent must temporarily lower that one register for the event and put it back
# afterwards — exactly the same atomic save/restore we already do for DESS Mode.
#
# Usage:
#   qw_dess_toggle.sh off      -> save DESS Mode, set Mode=0; if QW_MFRR_MIN_SOC
#                                 is set AND the live floor is higher, save the
#                                 floor and lower it to QW_MFRR_MIN_SOC.
#   qw_dess_toggle.sh on       -> restore saved DESS Mode (default 1) and the
#                                 saved SOC floor (if one was saved).
#   qw_dess_toggle.sh status   -> print live + saved mode/floor + off-age.
#
# Files:
#   $QW_STATE_DIR/qw_dess_saved_mode    — saved original DESS Mode (for restore)
#   $QW_STATE_DIR/qw_dess_saved_minsoc  — saved original SOC floor X (for restore)
#   /tmp/qw_dess_off_at                 — Unix ts when 'off' ran (for watchdog)
#
# Env overrides:
#   QW_STATE_DIR         (default /data)  — where the saved-state files live
#   QW_MFRR_MIN_SOC      (default empty)  — mFRR floor Y in %. EMPTY = never touch
#                                           the floor (backward compatible).
#   QW_MINSOC_DBUS_PATH  (default /Settings/CGwacs/BatteryLife/MinimumSocLimit)
#
# DESS Mode register 5400 is read-only over Modbus, so we use dbus throughout.
# =============================================================================

set -e

DBUS_SERVICE="com.victronenergy.settings"
MODE_PATH="/Settings/DynamicEss/Mode"
MINSOC_PATH="${QW_MINSOC_DBUS_PATH:-/Settings/CGwacs/BatteryLife/MinimumSocLimit}"
QW_STATE_DIR="${QW_STATE_DIR:-/data}"
SAVED_MODE_FILE="${QW_STATE_DIR}/qw_dess_saved_mode"
SAVED_MINSOC_FILE="${QW_STATE_DIR}/qw_dess_saved_minsoc"
OFF_AT_FILE="/tmp/qw_dess_off_at"
MFRR_MIN_SOC="${QW_MFRR_MIN_SOC:-}"
LOG_TAG="qw_dess"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  logger -t "$LOG_TAG" "$*"
}

# dbus_get <path> -> first number on the line (handles "1", "int32 1",
# "value = 20", "20.0"). Empty if the read fails.
dbus_get() {
  dbus -y "$DBUS_SERVICE" "$1" GetValue 2>/dev/null \
    | grep -oE '\-?[0-9]+(\.[0-9]+)?' \
    | head -n 1
}

dbus_set() {
  dbus -y "$DBUS_SERVICE" "$1" SetValue %"$2" >/dev/null 2>&1
}

# int_part <number> -> integer part only ("25.0" -> "25"), for safe sh compares.
int_part() {
  printf '%s' "${1%%.*}"
}

lower_floor_for_event() {
  # No-op unless a mFRR floor Y is configured.
  [ -n "$MFRR_MIN_SOC" ] || return 0

  # Already saved => we are mid-event; never overwrite the resting floor X.
  if [ -f "$SAVED_MINSOC_FILE" ]; then
    log "SOC floor already lowered for this event (saved=$(cat "$SAVED_MINSOC_FILE")); leaving it"
    return 0
  fi

  cur=$(dbus_get "$MINSOC_PATH")
  if [ -z "$cur" ]; then
    log "WARN: could not read SOC floor from $MINSOC_PATH; leaving floor untouched"
    return 0
  fi

  cur_i=$(int_part "$cur")
  y_i=$(int_part "$MFRR_MIN_SOC")
  if [ "$cur_i" -le "$y_i" ]; then
    log "SOC floor already ${cur_i}% (<= mFRR floor ${y_i}%); nothing to lower"
    return 0
  fi

  echo "$cur_i" > "$SAVED_MINSOC_FILE"
  dbus_set "$MINSOC_PATH" "$y_i"
  log "Lowered SOC floor ${cur_i}% -> ${y_i}% for mFRR (saved ${cur_i}% -> $SAVED_MINSOC_FILE)"
}

restore_floor_after_event() {
  [ -f "$SAVED_MINSOC_FILE" ] || return 0
  saved=$(cat "$SAVED_MINSOC_FILE")
  if [ -n "$saved" ]; then
    dbus_set "$MINSOC_PATH" "$saved"
    log "Restored SOC floor = ${saved}%"
  fi
  rm -f "$SAVED_MINSOC_FILE"
}

action="${1:-status}"

case "$action" in
  off)
    current=$(dbus_get "$MODE_PATH")
    if [ -z "$current" ]; then
      log "ERROR: could not read DESS Mode from dbus $MODE_PATH"
      exit 2
    fi
    if [ "$current" = "0" ]; then
      log "DESS already OFF (Mode=0); not overwriting saved mode"
    else
      echo "$current" > "$SAVED_MODE_FILE"
      log "Saved original DESS Mode = $current -> $SAVED_MODE_FILE"
      dbus_set "$MODE_PATH" 0
      log "Set DESS OFF (Mode=0)"
    fi
    # Lower the shared SOC floor so mFRR can discharge below the arbitrage floor.
    lower_floor_for_event
    date +%s > "$OFF_AT_FILE"
    exit 0
    ;;

  on)
    if [ -f "$SAVED_MODE_FILE" ]; then
      saved=$(cat "$SAVED_MODE_FILE")
    else
      saved=1
      log "WARN: $SAVED_MODE_FILE missing, restoring default DESS Mode = 1 (Auto)"
    fi
    dbus_set "$MODE_PATH" "$saved"
    log "Restored DESS Mode = $saved"
    # Put the arbitrage SOC floor back (safe no-op if we never lowered it).
    restore_floor_after_event
    rm -f "$OFF_AT_FILE"
    exit 0
    ;;

  status)
    current=$(dbus_get "$MODE_PATH")
    saved="(none)"
    [ -f "$SAVED_MODE_FILE" ] && saved=$(cat "$SAVED_MODE_FILE")
    floor=$(dbus_get "$MINSOC_PATH")
    saved_floor="(none)"
    [ -f "$SAVED_MINSOC_FILE" ] && saved_floor=$(cat "$SAVED_MINSOC_FILE")
    off_at="(none)"
    off_age="-"
    if [ -f "$OFF_AT_FILE" ]; then
      off_at=$(cat "$OFF_AT_FILE")
      now=$(date +%s)
      off_age="$((now - off_at)) s"
    fi
    echo "DESS Mode (live)    = $current"
    echo "Saved Mode          = $saved"
    echo "SOC floor (live)    = ${floor}%"
    echo "Saved SOC floor     = $saved_floor"
    echo "mFRR floor (Y)      = ${MFRR_MIN_SOC:-(unset -> floor untouched)}"
    echo "OFF since (epoch)   = $off_at"
    echo "OFF age             = $off_age"
    exit 0
    ;;

  *)
    echo "Usage: $0 {off|on|status}" >&2
    exit 1
    ;;
esac
