#!/bin/sh
# =============================================================================
# qw_log_audit.sh — turn silent agent-log symptoms into an alert
# =============================================================================
# Install to /data/qw_log_audit.sh (chmod 750) and run periodically, e.g. from
# /data/rc.local:
#
#   ( while true; do /data/qw_log_audit.sh >/dev/null 2>&1; sleep 3600; done ) &
#
# WHY: every defect this project has hit announced itself in the agent log and
# was still missed for weeks, because nobody reads the log.
#   * "ignoring non-FRR Mode"  — commands arriving and being dropped
#   * "nothing to lower"       — the two-level SOC floor never engaging (Y >= X)
#   * "FAILSAFE"               — an event truncated mid-delivery
#   * "no WORKMODE command"    — idle-refresh restarting the agent in a loop
#   * "subscription dead"      — the zombie-subscription watchdog firing
#   * "Traceback"              — the agent crashing and being restarted
#   * many "Starting qw_agent" — a restart loop of any origin
#
# Only lines that are NEW since the previous run are examined, so a one-off
# event is reported once instead of forever. Progress is a line count of
# multilog's `current`; when that file shrinks the log rotated and we start over.
#
# Findings go to stdout and to syslog (tag qw_health). Exit status:
#   0 = nothing, or only informational findings
#   1 = at least one WARN/ERROR finding (usable as a cron/monitor trigger)
#
# A deaf site produces NO new log lines at all, so the command-silence check runs
# unconditionally, keyed on the capture log's mtime rather than the agent log.
# That matters most where QW_IDLE_REFRESH_S is 0: with the restart backstop off,
# this warning is the only thing left that notices a site going quiet.
#
# Env overrides:
#   QW_LOG_DIR                (default /var/log/qw-agent)
#   QW_STATE_DIR              (default /data)  — where the progress file lives
#   QW_CAPTURE_LOG            (default /data/afrr-workmode.log)
#   QW_HEALTH_MAX_RESTARTS    (default 3)      — restarts per run before warning
#   QW_HEALTH_MAX_SILENCE_H   (default 36)     — command silence before warning
# =============================================================================

LOG_DIR="${QW_LOG_DIR:-/var/log/qw-agent}"
CUR="$LOG_DIR/current"
QW_STATE_DIR="${QW_STATE_DIR:-/data}"
OFFSET_FILE="${QW_STATE_DIR}/qw_health_offset"
CAPTURE="${QW_CAPTURE_LOG:-/data/afrr-workmode.log}"
MAX_RESTARTS="${QW_HEALTH_MAX_RESTARTS:-3}"
MAX_SILENCE_H="${QW_HEALTH_MAX_SILENCE_H:-36}"
LOG_TAG="qw_health"

findings=0
worst=0

# report <severity> <count> <message>
report() {
  sev=$1
  n=$2
  msg=$3
  [ "$n" -gt 0 ] || return 0
  findings=$((findings + 1))
  echo "qw_health: $sev x$n — $msg"
  logger -t "$LOG_TAG" "$sev x$n — $msg" 2>/dev/null
  case "$sev" in
    ERROR | WARN) worst=1 ;;
  esac
}

# --- new agent-log lines since the previous run ----------------------------- #

window=""
if [ ! -f "$CUR" ]; then
  echo "qw_health: no agent log at $CUR"
else
  total=$(wc -l < "$CUR" 2>/dev/null | tr -d ' ')
  [ -n "$total" ] || total=0

  prev=0
  if [ -f "$OFFSET_FILE" ]; then
    prev=$(cat "$OFFSET_FILE" 2>/dev/null)
    case "$prev" in
      '' | *[!0-9]*) prev=0 ;;
    esac
  fi
  # multilog rotated `current` away, so the old offset no longer maps to it.
  [ "$total" -lt "$prev" ] && prev=0

  new=$((total - prev))
  printf '%s\n' "$total" > "$OFFSET_FILE"

  if [ "$new" -le 0 ]; then
    echo "qw_health: no new log lines (at line $total)"
  else
    window=$(tail -n "$new" "$CUR" 2>/dev/null)
  fi
fi

# count <pattern> -> matching lines in the new window (0 when there are none)
count() {
  [ -n "$window" ] || { echo 0; return 0; }
  printf '%s\n' "$window" | grep -c "$1" 2>/dev/null || true
}

report ERROR "$(count 'Traceback')" \
  "agent crashed (Traceback); check the lines above it for the cause"
report ERROR "$(count 'FAILSAFE')" \
  "failsafe reverted an event — it either ran past QW_MAX_EVENT_S or lost the QW link mid-delivery"
report ERROR "$(count 'subscription dead')" \
  "zombie subscription: transport was connected but the command topic was not"
report ERROR "$(count 'REJECT')" \
  "setpoint rejected by the clamp — dispatch was NOT delivered; check QW_MAX_IMPORT_W / QW_MAX_EXPORT_W against what the market actually sends"
report WARN "$(count 'nothing to lower')" \
  "two-level SOC floor did not engage — QW_MFRR_MIN_SOC must be BELOW the dashboard Minimum SOC"
report WARN "$(count 'no WORKMODE command received')" \
  "idle-refresh restarted the agent; if this repeats, QW_IDLE_REFRESH_S is below this site's real command silence"
report WARN "$(count 'link down for')" \
  "QW link stayed down past QW_LINK_RESTART_S"
report WARN "$(count 'ignoring non-FRR Mode')" \
  "commands from a trusted source were dropped by the mode gate (expected for Mode=buy)"
report WARN "$(count 'QW connect attempt')" \
  "initial QW connect failed and was retried (usually DNS not ready at boot)"

restarts=$(count 'Starting qw_agent')
if [ "$restarts" -gt "$MAX_RESTARTS" ]; then
  report WARN "$restarts" \
    "agent restarted more than $MAX_RESTARTS times in this window"
fi

# --- command silence (runs even when nothing was logged at all) ------------- #

if [ -f "$CAPTURE" ] && [ "$MAX_SILENCE_H" -gt 0 ]; then
  mtime=$(date -r "$CAPTURE" +%s 2>/dev/null || stat -c %Y "$CAPTURE" 2>/dev/null)
  case "$mtime" in
    '' | *[!0-9]*) mtime='' ;;
  esac
  if [ -n "$mtime" ]; then
    age_h=$(( ($(date +%s) - mtime) / 3600 ))
    if [ "$age_h" -ge "$MAX_SILENCE_H" ]; then
      report WARN 1 \
        "no WORKMODE command for ${age_h}h (>= ${MAX_SILENCE_H}h) — either the market is quiet or the site is silently deaf; check that telemetry still reaches the portal"
    fi
  fi
fi

if [ "$findings" -eq 0 ]; then
  echo "qw_health: OK — nothing to report"
fi

exit "$worst"
