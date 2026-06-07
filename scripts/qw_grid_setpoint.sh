#!/bin/sh
# =============================================================================
# qw_grid_setpoint.sh — write the ESS grid power setpoint (mFRR actuator)
# =============================================================================
# Install to /data/qw_grid_setpoint.sh. Permissions: chmod 750
#
# Writes /Settings/CGwacs/AcPowerSetPoint via dbus. This is the official Victron
# mechanism for external grid power control — the ESS Assistant inside the
# MultiPlus-II distributes it across phases, honouring grid current limits and
# BMS constraints.
#
# Usage:
#   qw_grid_setpoint.sh 28000     -> import 28 kW (frrdown)
#   qw_grid_setpoint.sh -15000    -> export 15 kW (frrup)
#   qw_grid_setpoint.sh 0         -> release (self-consumption baseline)
#   qw_grid_setpoint.sh get       -> read current value
#
# Env overrides (ASYMMETRIC limits — import and export differ):
#   QW_MAX_IMPORT_W   (default 15000) — max positive setpoint  (frrdown / import)
#   QW_MAX_EXPORT_W   (default 15000) — max |negative| setpoint (frrup / export)
#
# Why asymmetric: a site's grid-connection import capacity often exceeds its
# allowed export (feed-in) cap. e.g. site A: import up to 28 kW (under the
# ~34.5 kW 3x50A connection), export capped at the 15 kW feed-in limit. A single
# symmetric clamp set to the export cap would WRONGLY reject legitimate >15 kW
# frrdown (import) signals from Qilowatt.
#
# SAFETY: requests beyond the per-direction limit are rejected (exit 3) and the
# setpoint is not written. Set the limits to your site's grid connection
# capacity / feed-in cap. See docs/SAFETY.md.
# =============================================================================

set -e

DBUS_SERVICE="com.victronenergy.settings"
DBUS_PATH="/Settings/CGwacs/AcPowerSetPoint"
MAX_IMPORT_W="${QW_MAX_IMPORT_W:-15000}"   # positive setpoint = import (frrdown)
MAX_EXPORT_W="${QW_MAX_EXPORT_W:-15000}"   # |negative| setpoint = export (frrup)
LOG_TAG="qw_grid_sp"

log() {
  logger -t "$LOG_TAG" "$*"
}

dbus_get() {
  dbus -y "$DBUS_SERVICE" "$DBUS_PATH" GetValue 2>/dev/null \
    | grep -oE '\-?[0-9]+(\.[0-9]+)?' \
    | head -n 1
}

dbus_set() {
  val="$1"
  dbus -y "$DBUS_SERVICE" "$DBUS_PATH" SetValue %"$val" >/dev/null 2>&1
}

arg="${1:-}"

if [ -z "$arg" ]; then
  echo "Usage: $0 <watts|get>"
  echo "  watts: -${MAX_EXPORT_W} (export) ... +${MAX_IMPORT_W} (import)"
  exit 1
fi

if [ "$arg" = "get" ]; then
  cur=$(dbus_get)
  echo "AcPowerSetPoint = ${cur} W"
  exit 0
fi

# Must be a (possibly negative) integer.
if ! echo "$arg" | grep -qE '^-?[0-9]+$'; then
  echo "ERROR: '$arg' is not an integer" >&2
  exit 2
fi

# Asymmetric clamp: import (positive) up to MAX_IMPORT_W, export (negative) up
# to MAX_EXPORT_W in magnitude.
case "$arg" in
  -*)
    abs_val=${arg#-}
    if [ "$abs_val" -gt "$MAX_EXPORT_W" ]; then
      echo "ERROR: export |$arg| > $MAX_EXPORT_W W (export limit exceeded)" >&2
      log "REJECT: $arg W (export over $MAX_EXPORT_W)"
      exit 3
    fi
    ;;
  *)
    if [ "$arg" -gt "$MAX_IMPORT_W" ]; then
      echo "ERROR: import $arg > $MAX_IMPORT_W W (import limit exceeded)" >&2
      log "REJECT: $arg W (import over $MAX_IMPORT_W)"
      exit 3
    fi
    ;;
esac

old=$(dbus_get)
dbus_set "$arg"
new=$(dbus_get)
log "AcPowerSetPoint: $old -> $new W"
echo "AcPowerSetPoint: $old -> $new W"
