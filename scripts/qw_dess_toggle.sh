#!/bin/sh
# =============================================================================
# qw_dess_toggle.sh — toggle Dynamic ESS off/on for the duration of an mFRR event
# =============================================================================
# Install to /data/qw_dess_toggle.sh (persists across Venus OS firmware updates).
# Permissions: chmod 750
#
# Usage:
#   qw_dess_toggle.sh off      -> save current DESS Mode, set Mode=0 (off)
#   qw_dess_toggle.sh on       -> restore previously saved DESS Mode (default 1)
#   qw_dess_toggle.sh status   -> print live + saved mode + off-age timestamps
#
# Files:
#   $QW_STATE_DIR/qw_dess_saved_mode  — saved original DESS Mode (for restore)
#   /tmp/qw_dess_off_at               — Unix ts when 'off' ran (for watchdog)
#
# Env overrides:
#   QW_STATE_DIR   (default /data)   — where the saved-mode file lives
#
# DESS Mode register 5400 is read-only over Modbus, so we use dbus.
# =============================================================================

set -e

DBUS_SERVICE="com.victronenergy.settings"
DBUS_PATH="/Settings/DynamicEss/Mode"
QW_STATE_DIR="${QW_STATE_DIR:-/data}"
SAVED_FILE="${QW_STATE_DIR}/qw_dess_saved_mode"
OFF_AT_FILE="/tmp/qw_dess_off_at"
LOG_TAG="qw_dess"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  logger -t "$LOG_TAG" "$*"
}

dbus_get() {
  # dbus output varies by firmware: "1", "int32 1", or "value = 1".
  # Take the first integer (may be negative).
  dbus -y "$DBUS_SERVICE" "$DBUS_PATH" GetValue 2>/dev/null \
    | grep -oE '\-?[0-9]+' \
    | head -n 1
}

dbus_set() {
  val="$1"
  dbus -y "$DBUS_SERVICE" "$DBUS_PATH" SetValue %"$val" >/dev/null 2>&1
}

action="${1:-status}"

case "$action" in
  off)
    current=$(dbus_get)
    if [ -z "$current" ]; then
      log "ERROR: could not read DESS Mode from dbus $DBUS_PATH"
      exit 2
    fi
    if [ "$current" = "0" ]; then
      log "DESS already OFF (Mode=0); not overwriting saved mode"
    else
      echo "$current" > "$SAVED_FILE"
      log "Saved original DESS Mode = $current -> $SAVED_FILE"
      dbus_set 0
      log "Set DESS OFF (Mode=0)"
    fi
    date +%s > "$OFF_AT_FILE"
    exit 0
    ;;

  on)
    if [ -f "$SAVED_FILE" ]; then
      saved=$(cat "$SAVED_FILE")
    else
      saved=1
      log "WARN: $SAVED_FILE missing, restoring default DESS Mode = 1 (Auto)"
    fi
    dbus_set "$saved"
    log "Restored DESS Mode = $saved"
    rm -f "$OFF_AT_FILE"
    exit 0
    ;;

  status)
    current=$(dbus_get)
    saved="(none)"
    [ -f "$SAVED_FILE" ] && saved=$(cat "$SAVED_FILE")
    off_at="(none)"
    off_age="-"
    if [ -f "$OFF_AT_FILE" ]; then
      off_at=$(cat "$OFF_AT_FILE")
      now=$(date +%s)
      off_age="$((now - off_at)) s"
    fi
    echo "DESS Mode (live)    = $current"
    echo "Saved Mode          = $saved"
    echo "OFF since (epoch)   = $off_at"
    echo "OFF age             = $off_age"
    exit 0
    ;;

  *)
    echo "Usage: $0 {off|on|status}" >&2
    exit 1
    ;;
esac
