#!/bin/sh
# =============================================================================
# qw_doctor.sh — "is this Cerbo fit for the Qilowatt agent, and set up right?"
# =============================================================================
# Run ON the Cerbo (deploy/install.sh runs it at the end; issue reports should
# paste its output). Read-only: it never writes a dbus value or a file.
#
#   /data/qw_doctor.sh            # PASS / WARN / FAIL lines, exit 1 on any FAIL
#
# Checks, in order:
#   runtime   python3 + python3-dbus, vendored qilowatt/paho importable
#   dbus      the paths the agent reads and writes exist on this Venus build
#   ESS/DESS  DESS installed and its Mode; ESS not in "Keep batteries charged"
#   env       /data/qw-agent.env present, private, no REPLACE_WITH_* left,
#             import/export caps set and consistent with MaxFeedInPower /
#             AC input current limit (suggests values), event caps below the
#             DESS watchdog cap, QW_MFRR_MIN_SOC below the live floor
#   state     leftover DESS-off / saved Mode from a crashed run, state.json
#   service   /service/qw-agent linked to /data and up; watchdog, audit and
#             capture loops actually running (not just listed in rc.local)
#   time      clock sane (TLS to the broker fails on a 1970 clock)
#   log       recent actuation failed / telemetry unavailable / Traceback
#
# Env overrides (mostly for tests):
#   QW_AGENT_ENV, QW_AGENT_DIR, QW_STATE_DIR, QW_SERVICE_LINK, QW_RC_LOCAL,
#   QW_LOG_DIR, QW_DOCTOR_SKIP=python,ps,time,service   (comma list)
# =============================================================================

ENV_FILE="${QW_AGENT_ENV:-/data/qw-agent.env}"
AGENT_DIR="${QW_AGENT_DIR:-/data/qw-agent}"
QW_STATE_DIR="${QW_STATE_DIR:-/data}"
SERVICE_LINK="${QW_SERVICE_LINK:-/service/qw-agent}"
RC_LOCAL="${QW_RC_LOCAL:-/data/rc.local}"
LOG_DIR="${QW_LOG_DIR:-/var/log/qw-agent}"
OFF_AT_FILE="${QW_OFF_AT_FILE:-/tmp/qw_dess_off_at}"
SKIP=",${QW_DOCTOR_SKIP:-},"

SETTINGS=com.victronenergy.settings
SYSTEM=com.victronenergy.system

pass=0; warn=0; fail=0
PASS() { pass=$((pass + 1)); echo "PASS  $*"; }
WARN() { warn=$((warn + 1)); echo "WARN  $*"; }
FAIL() { fail=$((fail + 1)); echo "FAIL  $*"; }
skipped() { case "$SKIP" in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# dbus_get SERVICE PATH -> first number, empty when unreadable
dbus_get() {
  dbus -y "$1" "$2" GetValue 2>/dev/null | grep -oE '\-?[0-9]+(\.[0-9]+)?' | head -n 1
}
int_part() { printf '%s' "${1%%.*}"; }
is_num() { printf '%s' "$1" | grep -qE '^-?[0-9]+(\.[0-9]+)?$'; }

# env_val KEY -> value from the env file (last assignment wins, quotes stripped)
env_val() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^[[:space:]]*$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 \
    | sed -e 's/^[^=]*=//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

echo "qw_doctor: $(date '+%Y-%m-%d %H:%M:%S') on $(hostname 2>/dev/null || echo ?)"
[ -f "$AGENT_DIR/VERSION" ] && echo "agent install: $(cat "$AGENT_DIR/VERSION")"

# --- runtime --------------------------------------------------------------- #
if ! skipped python; then
  if command -v python3 >/dev/null 2>&1; then
    pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
    PASS "python3 $pyver"
    if python3 -c 'import dbus' 2>/dev/null; then
      PASS "python3-dbus importable"
    else
      FAIL "python3 cannot 'import dbus' — install python3-dbus (opkg) or telemetry will never be published"
    fi
    if PYTHONPATH="$AGENT_DIR/pylib" python3 -c 'import qilowatt, paho.mqtt.client' 2>/dev/null; then
      PASS "vendored qilowatt + paho importable from $AGENT_DIR/pylib"
    else
      FAIL "cannot import qilowatt/paho from $AGENT_DIR/pylib — re-run deploy/install.sh"
    fi
  else
    FAIL "python3 not found"
  fi
fi

# --- dbus paths ------------------------------------------------------------ #
if ! command -v dbus >/dev/null 2>&1; then
  FAIL "dbus CLI not found — is this Venus OS?"
else
  for p in /Settings/CGwacs/AcPowerSetPoint /Settings/CGwacs/BatteryLife/MinimumSocLimit; do
    v=$(dbus_get "$SETTINGS" "$p")
    if [ -n "$v" ]; then PASS "$SETTINGS $p = $v"; else FAIL "$SETTINGS $p unreadable — ESS not configured?"; fi
  done
  for p in /Dc/Battery/Soc /Ac/Grid/L1/Power; do
    v=$(dbus_get "$SYSTEM" "$p")
    if [ -n "$v" ]; then
      PASS "$SYSTEM $p = $v"
    elif [ "$p" = /Dc/Battery/Soc ]; then
      FAIL "$SYSTEM $p unreadable — no battery monitor; the agent publishes NO telemetry without SOC"
    else
      WARN "$SYSTEM $p unreadable — no grid meter? ENERGY.Power will be 0 (a Multi-measured grid needs 'Grid meter: inverter/charger')"
    fi
  done

  # DESS
  dess=$(dbus_get "$SETTINGS" /Settings/DynamicEss/Mode)
  if [ -z "$dess" ]; then
    FAIL "/Settings/DynamicEss/Mode unreadable — Dynamic ESS not present on this firmware; qw_dess_toggle.sh cannot work"
  else
    case "$(int_part "$dess")" in
      0) if [ -f "$QW_STATE_DIR/qw_dess_saved_mode" ]; then
           WARN "DESS Mode = 0 (off) with a saved Mode on disk — an event is running or a previous run died mid-event (agent restores it at start-up)"
         else
           WARN "DESS Mode = 0 (off) — the agent will restore Mode 1 after each event; if you do not use DESS at all that is fine"
         fi ;;
      *) PASS "DESS Mode = $dess" ;;
    esac
  fi

  # ESS mode / BatteryLife state: 9 = Keep batteries charged
  bl_state=$(dbus_get "$SETTINGS" /Settings/CGwacs/BatteryLife/State)
  if [ -n "$bl_state" ]; then
    if [ "$(int_part "$bl_state")" = 9 ]; then
      FAIL "ESS is in 'Keep batteries charged' (BatteryLife/State=9) — the grid setpoint will not discharge the battery; use Optimized (with/without BatteryLife)"
    else
      PASS "ESS BatteryLife/State = $bl_state (not 'keep charged')"
    fi
  fi
  hub4=$(dbus_get "$SETTINGS" /Settings/CGwacs/Hub4Mode)
  [ -n "$hub4" ] && [ "$(int_part "$hub4")" = 3 ] && WARN "Hub4Mode = 3 (External control) — another controller may own AcPowerSetPoint"

  feedin=$(dbus_get "$SETTINGS" /Settings/CGwacs/MaxFeedInPower)
  floor=$(dbus_get "$SETTINGS" /Settings/CGwacs/BatteryLife/MinimumSocLimit)
fi

# --- env file --------------------------------------------------------------- #
if [ ! -f "$ENV_FILE" ]; then
  FAIL "$ENV_FILE missing — copy .env.example there and fill in QW_DEVICE_ID / QW_MQTT_USER / QW_MQTT_PASS"
else
  perms=$(stat -c %a "$ENV_FILE" 2>/dev/null || echo ?)
  case "$perms" in
    600|400) PASS "$ENV_FILE mode $perms" ;;
    *) WARN "$ENV_FILE mode $perms — contains credentials; chmod 600" ;;
  esac
  if grep -q 'REPLACE_WITH' "$ENV_FILE"; then
    FAIL "$ENV_FILE still has REPLACE_WITH_* placeholders: $(grep -oE 'REPLACE_WITH_[A-Z_]+' "$ENV_FILE" | tr '\n' ' ')"
  else
    PASS "no REPLACE_WITH_* placeholders"
  fi
  for k in QW_DEVICE_ID QW_MQTT_USER QW_MQTT_PASS; do
    [ -n "$(env_val $k)" ] || FAIL "$k is empty in $ENV_FILE"
  done
  did=$(env_val QW_DEVICE_ID)
  if [ -n "$did" ] && ! printf '%s' "$did" | grep -qiE '^[0-9a-f-]{36}$'; then
    WARN "QW_DEVICE_ID '$did' is not a UUID — it must be the inverter_id (UUID), not the account device_id hex"
  fi

  imp=$(env_val QW_MAX_IMPORT_W); exp=$(env_val QW_MAX_EXPORT_W)
  if [ -z "$imp" ] || [ -z "$exp" ]; then
    WARN "QW_MAX_IMPORT_W / QW_MAX_EXPORT_W not set — the agent does not cap, the setpoint script REJECTS anything over 15000 W"
  elif [ "$imp" = 15000 ] && [ "$exp" = 15000 ]; then
    WARN "QW_MAX_IMPORT_W and QW_MAX_EXPORT_W are the example 15000 — confirm against your grid connection"
  else
    PASS "caps import=$imp W export=$exp W"
  fi
  if [ -n "${feedin:-}" ] && is_num "$feedin" && [ "$(int_part "$feedin")" -ge 0 ] && [ -n "$exp" ] && is_num "$exp"; then
    if [ "$(int_part "$exp")" -gt "$(int_part "$feedin")" ]; then
      WARN "QW_MAX_EXPORT_W=$exp exceeds ESS MaxFeedInPower=$feedin W — the inverter will not export more than $feedin; set QW_MAX_EXPORT_W<=$(int_part "$feedin")"
    else
      PASS "QW_MAX_EXPORT_W=$exp <= MaxFeedInPower=$feedin"
    fi
  fi
  # Suggest an import cap from the AC input current limit (per phase) x 230 V.
  if command -v dbus >/dev/null 2>&1; then
    vebus=$(dbus -y 2>/dev/null | grep -m1 '^com.victronenergy.vebus')
    if [ -n "$vebus" ]; then
      ilim=$(dbus_get "$vebus" /Ac/ActiveIn/CurrentLimit)
      nph=$(dbus_get "$vebus" /Ac/NumberOfPhases)
      [ -n "$nph" ] || nph=1
      if [ -n "$ilim" ] && is_num "$ilim"; then
        sugg=$(( $(int_part "$ilim") * 230 * $(int_part "$nph") ))
        echo "info  AC input current limit ${ilim} A x ${nph} ph -> ~${sugg} W; a sane QW_MAX_IMPORT_W is at or below this"
        if [ -n "$imp" ] && is_num "$imp" && [ "$(int_part "$imp")" -gt "$sugg" ]; then
          WARN "QW_MAX_IMPORT_W=$imp is above the AC input limit (~$sugg W)"
        fi
      fi
    fi
  fi

  # event caps vs watchdog
  maxoff=$(env_val QW_MAX_OFF_SECS); [ -n "$maxoff" ] || maxoff=7800
  ev=$(env_val QW_MAX_EVENT_S); [ -n "$ev" ] || ev=7200
  tr_=$(env_val QW_MAX_TRADE_S); [ -n "$tr_" ] || tr_=5400
  for pair in "QW_MAX_EVENT_S=$ev" "QW_MAX_TRADE_S=$tr_"; do
    val=${pair#*=}
    if is_num "$val" && [ "$(int_part "$val")" -lt "$(int_part "$maxoff")" ]; then
      PASS "$pair < watchdog QW_MAX_OFF_SECS=$maxoff"
    else
      FAIL "$pair is not below the DESS watchdog cap QW_MAX_OFF_SECS=$maxoff"
    fi
  done

  # two-level floor
  msoc=$(env_val QW_MFRR_MIN_SOC)
  if [ -n "$msoc" ]; then
    if [ -n "${floor:-}" ] && is_num "$msoc"; then
      if [ "$(int_part "$msoc")" -lt "$(int_part "$floor")" ]; then
        PASS "QW_MFRR_MIN_SOC=$msoc below live ESS floor $floor %"
      else
        WARN "QW_MFRR_MIN_SOC=$msoc is NOT below the live ESS floor $floor % — the floor is never lowered ('nothing to lower')"
      fi
    fi
  else
    echo "info  QW_MFRR_MIN_SOC unset — single SOC floor (the dashboard slider) for arbitrage and mFRR"
  fi

  srcs=$(env_val QW_MFRR_SOURCES)
  [ -n "$srcs" ] && ! printf ',%s,' "$srcs" | grep -q ',qilowatt,' && \
    WARN "QW_MFRR_SOURCES=$srcs excludes 'qilowatt' — a site dispatched directly by Qilowatt would drop every activation (the log then says 'dropping FRR dispatch')"
  [ "$(env_val QW_DRY_RUN)" = 1 ] && WARN "QW_DRY_RUN=1 — the agent logs intentions only and actuates NOTHING"
  [ -n "$(env_val QW_TELEMETRY_PROFILE)" ] && [ "$(env_val QW_TELEMETRY_PROFILE)" != auto ] && \
    echo "info  QW_TELEMETRY_PROFILE=$(env_val QW_TELEMETRY_PROFILE) (legacy alias; 'auto' fits every topology)"
fi

# --- leftover state ---------------------------------------------------------- #
if [ -f "$QW_STATE_DIR/qw_dess_saved_mode" ] || [ -f "$OFF_AT_FILE" ]; then
  echo "info  event state files present: $(ls "$QW_STATE_DIR"/qw_dess_saved_* "$OFF_AT_FILE" 2>/dev/null | tr '\n' ' ')"
fi
if [ -f "$AGENT_DIR/state.json" ]; then
  st=$(cat "$AGENT_DIR/state.json" 2>/dev/null)
  echo "info  state.json: $st"
  case "$st" in
    *'"state": "IDLE"'*)
      if [ -f "$QW_STATE_DIR/qw_dess_saved_mode" ]; then
        WARN "agent says IDLE but a saved DESS Mode exists — leftover from a crash; the agent recovers it at its next start"
      fi ;;
    *'"degraded": true'*) WARN "state.json reports degraded=true — the last actuator write did not read back" ;;
  esac
fi

# --- service + loops -------------------------------------------------------- #
if ! skipped service; then
  target=$(readlink "$SERVICE_LINK" 2>/dev/null)
  if [ "$target" = "$AGENT_DIR/service/qw-agent" ]; then
    PASS "$SERVICE_LINK -> $target"
  elif [ -z "$target" ]; then
    FAIL "$SERVICE_LINK missing — run deploy/install.sh (or /data/rc.local)"
  else
    WARN "$SERVICE_LINK -> $target (expected $AGENT_DIR/service/qw-agent; a rootfs copy is wiped by firmware updates)"
  fi
  if command -v svstat >/dev/null 2>&1; then
    sv=$(svstat "$SERVICE_LINK" 2>/dev/null)
    case "$sv" in
      *": up "*) PASS "service $sv" ;;
      *) FAIL "service not up: ${sv:-svstat failed}" ;;
    esac
  fi
  if [ -f "$RC_LOCAL" ]; then
    for hook in qw_dess_watchdog qw_log_audit 'afrr_capture.sh start' 'qw-agent/service/qw-agent'; do
      grep -q "$hook" "$RC_LOCAL" || WARN "$RC_LOCAL lacks the '$hook' hook — re-run deploy/install.sh"
    done
  else
    WARN "$RC_LOCAL missing — nothing re-links the service or starts the watchdog after a reboot/firmware update"
  fi
fi
if ! skipped ps; then
  for loop in qw_dess_watchdog qw_log_audit afrr_capture; do
    if ps 2>/dev/null | grep -v grep | grep -q "$loop"; then
      PASS "$loop loop running"
    elif [ "$loop" = qw_dess_watchdog ]; then
      FAIL "$loop loop NOT running — the outer DESS-off backstop is missing; run: nohup sh -c 'while true; do /data/qw_dess_watchdog.sh; sleep 60; done' &"
    else
      WARN "$loop loop not running (rc.local hook present but never started? reboot or start it by hand)"
    fi
  done
fi

# --- time ------------------------------------------------------------------- #
if ! skipped time; then
  now=$(date +%s)
  if [ "$now" -lt 1700000000 ]; then
    FAIL "clock is $(date) — TLS to mqtt.qilowatt.it fails until NTP syncs"
  else
    PASS "clock $(date '+%Y-%m-%d %H:%M:%S %Z')"
  fi
fi

# --- recent log ------------------------------------------------------------- #
if [ -f "$LOG_DIR/current" ]; then
  recent=$(tail -n 500 "$LOG_DIR/current" 2>/dev/null)
  for pat in 'actuation failed' 'telemetry unavailable' 'Traceback' 'REJECT' 'CONFIG WARN'; do
    n=$(printf '%s\n' "$recent" | grep -c "$pat" 2>/dev/null || true)
    [ "${n:-0}" -gt 0 ] && WARN "$n x '$pat' in the last 500 log lines — see $LOG_DIR/current"
  done
  last_start=$(printf '%s\n' "$recent" | grep 'Starting qw_agent' | tail -n 1)
  [ -n "$last_start" ] && echo "info  last start: $last_start"
else
  [ -d "$LOG_DIR" ] || WARN "$LOG_DIR missing — the service log/run cannot write; created on boot by rc.local"
fi

echo "-----------------------------------------"
echo "qw_doctor: $pass PASS, $warn WARN, $fail FAIL"
[ "$fail" -eq 0 ]
