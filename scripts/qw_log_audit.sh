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
#   * "ignoring non-FRR Mode"  — commands arriving and being dropped (buy/sell
#                                here means Q trades are disabled on this site)
#   * "nothing to lower"       — the two-level SOC floor never engaging (Y >= X)
#   * "FAILSAFE"               — an event truncated mid-delivery
#   * a foreign mFRR/TRADE END — a third party writing our WorkMode channel
#   * "TRADE START"            — Q trades executed (informational count)
#   * "no WORKMODE command"    — idle-refresh restarting the agent in a loop
#   * "subscription dead"      — the zombie-subscription watchdog firing
#   * "Traceback"              — the agent crashing and being restarted
#   * many "Starting qw_agent" — a restart loop of any origin
#   * "actuation failed"       — a setpoint/DESS write did not read back
#   * "telemetry unavailable"  — dbus gone, no SENSOR published (portal offline)
#   * "STARTUP RECOVERY"       — a previous process died mid-event
#   * "CONFIG WARN"            — start-up config sanity check (caps, floor)
#   * "dropping FRR dispatch"  — frrup/frrdown from a source not in QW_MFRR_SOURCES
#   * trades with no mFRR      — Q pre-charging with no dispatch following it
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
#   QW_HEALTH_MIN_TRADES_NO_FRR (default 3)    — trades w/o any mFRR START -> WARN
#   QW_ALERT_URL              (default empty)  — if set (here or in
#                             /data/qw-agent.env), WARN/ERROR findings are POSTed
#                             there as text/plain with curl (ntfy, Slack/Discord
#                             webhook, Home Assistant webhook ...). Off by default.
#   QW_AGENT_ENV              (default /data/qw-agent.env) — where QW_ALERT_URL is read from
# =============================================================================

LOG_DIR="${QW_LOG_DIR:-/var/log/qw-agent}"
CUR="$LOG_DIR/current"
QW_STATE_DIR="${QW_STATE_DIR:-/data}"
OFFSET_FILE="${QW_STATE_DIR}/qw_health_offset"
CAPTURE="${QW_CAPTURE_LOG:-/data/afrr-workmode.log}"
MAX_RESTARTS="${QW_HEALTH_MAX_RESTARTS:-3}"
MAX_SILENCE_H="${QW_HEALTH_MAX_SILENCE_H:-36}"
LOG_TAG="qw_health"
ENV_FILE="${QW_AGENT_ENV:-/data/qw-agent.env}"
ALERT_URL="${QW_ALERT_URL:-}"
if [ -z "$ALERT_URL" ] && [ -f "$ENV_FILE" ]; then
  ALERT_URL=$(grep -E '^[[:space:]]*QW_ALERT_URL=' "$ENV_FILE" 2>/dev/null | tail -n 1 \
    | sed -e 's/^[^=]*=//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")
fi

findings=0
worst=0
alert_body=""

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
    ERROR | WARN)
      worst=1
      alert_body="${alert_body}${sev} x${n} — ${msg}
" ;;
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
report ERROR "$(count 'actuation failed')" \
  "an actuator write did not read back (setpoint or DESS Mode) even after a retry — the event was tracked but NOT delivered; check dbus, qw_grid_setpoint.sh get, qw_dess_toggle.sh status"
report ERROR "$(count 'telemetry unavailable')" \
  "dbus/SOC unreadable — no SENSOR was published and the portal shows the device offline; run qw_doctor.sh"
report WARN "$(count 'STARTUP RECOVERY')" \
  "the agent found DESS off / a saved Mode from a previous run at start-up and restored normal — a crash, kill -9 or reboot happened mid-event"
report WARN "$(count 'CONFIG WARN')" \
  "start-up configuration check flagged something — grep 'CONFIG WARN' for the exact finding"
report WARN "$(count 'dropping FRR dispatch')" \
  "frrup/frrdown arrived from a source NOT in QW_MFRR_SOURCES and was dropped — if that source is your dispatcher, add it"
report WARN "$(count 'nothing to lower')" \
  "two-level SOC floor did not engage — QW_MFRR_MIN_SOC must be BELOW the dashboard Minimum SOC"
report WARN "$(count 'no WORKMODE command received')" \
  "idle-refresh restarted the agent; if this repeats, QW_IDLE_REFRESH_S is below this site's real command silence"
report WARN "$(count 'link down for')" \
  "QW link stayed down past QW_LINK_RESTART_S"
report WARN "$(count 'ignoring non-FRR Mode')" \
  "commands from a trusted source were dropped by the mode gate — Mode=buy/sell here means Q trades are DISABLED (QW_TRADE_MODES); savebattery/limitexport/normal are dropped by design"

# --- Q trades (informational) ----------------------------------------------- #
# A trade is the vendor refilling the battery between mFRR activations. Its
# START/END pairs should roughly match, and most ends should be either the SOC
# target or the FRR dispatch that follows. Lots of trades with no FRR after them
# is worth a look at the portal (is the site still being dispatched?).
report INFO "$(count 'TRADE START')" \
  "Q trades started (buy = import to the BatterySoc target, sell = export)"
report INFO "$(count 'trade target reached')" \
  "Q trades ended on the live SOC reaching the commanded target"
report INFO "$(count 'capping ')" \
  "requested PowerLimit was capped to QW_MAX_IMPORT_W / QW_MAX_EXPORT_W before dispatch"
# Economic sanity: a Q buy costs energy and only pays off through the mFRR
# activation it prepares for. Several trades and not one dispatch in the same
# window means the site is being pre-charged but not (or no longer) dispatched.
trades=$(count 'TRADE START')
frr_starts=$(count 'mFRR START')
MIN_TRADES_NO_FRR="${QW_HEALTH_MIN_TRADES_NO_FRR:-3}"
if [ "$trades" -ge "$MIN_TRADES_NO_FRR" ] && [ "$frr_starts" -eq 0 ]; then
  report WARN "$trades" \
    "Q trades started but NO mFRR dispatch in the same window — the battery is being bought full without an activation to sell into; check the portal"
fi
report WARN "$(count 'QW connect attempt')" \
  "initial QW connect failed and was retried (usually DNS not ready at boot)"

# --- who ended our events -------------------------------------------------- #
# An event should end because the dispatcher stood down (a 0 W FRR command),
# because the portal returned the site to normal, or because a failsafe fired
# (already reported above). Anything else is a third party writing the same
# WorkMode channel and cutting delivery short — which is exactly how Qilowatt's
# Energy Optimizer truncated every event on both sites on 2026-07-27 while
# looking, in the log, like a perfectly ordinary end of dispatch.
if [ -n "$window" ]; then
  # Only an event Mode (frrup/frrdown, buy/sell) at 0 W is a stand-down. A
  # *non*-event Mode at 0 W is not exempt: that is precisely what the
  # Optimizer's limitexport/savebattery looked like. A trade ending on its SOC
  # target is the agent's own doing. A `qilowatt/buy` ending an mFRR event can
  # only happen with Q trades disabled — then it is a configuration finding,
  # not interference, but still worth seeing.
  foreign=$(printf '%s\n' "$window" \
    | grep -E '(mFRR|TRADE) END \(' \
    | grep -v -E 'END \(notimer/' \
    | grep -v '/frrup 0 W)' \
    | grep -v '/frrdown 0 W)' \
    | grep -v '/buy 0 W)' \
    | grep -v '/sell 0 W)' \
    | grep -v 'trade target reached' \
    | grep -v 'failsafe' \
    | grep -v 'agent shutdown' \
    | grep -c . 2>/dev/null || true)
  report WARN "$foreign" \
    "an event was ended by a foreign automation writing the same WorkMode channel — grep 'END (' to see which one (qilowatt/buy here = Q trades disabled)"
fi

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

# --- optional push notification -------------------------------------------- #
if [ "$worst" -eq 1 ] && [ -n "$ALERT_URL" ] && command -v curl >/dev/null 2>&1; then
  host=$(hostname 2>/dev/null || echo cerbo)
  if curl -fsS -m 15 -X POST -H 'Content-Type: text/plain' \
       --data-binary "qw_health @ ${host}
${alert_body}" "$ALERT_URL" >/dev/null 2>&1; then
    echo "qw_health: alert sent to QW_ALERT_URL"
  else
    echo "qw_health: alert POST to QW_ALERT_URL failed"
  fi
fi

exit "$worst"
