#!/usr/bin/env bash
# =============================================================================
# install.sh — deploy cerbo-qilowatt-mfrr to a Cerbo GX over SSH
# =============================================================================
# Run from a workstation (NOT on the Cerbo). Pure-Python deps are vendored
# locally and copied over, so the Cerbo needs no pip/internet. dbus access uses
# the Cerbo's system python3-dbus.
#
# Usage:
#   CERBO_HOST=root@192.168.1.232 ./deploy/install.sh [options]
#
#   --restart            svc -d / svc -u the agent after deploying (default: the
#                        running agent keeps its old code until you restart it)
#   --dry-run-window N   stop the service, run the agent for N seconds with
#                        QW_DRY_RUN=1 (logs intentions, touches nothing), then
#                        restore the service. Prints the dry-run log.
#   --uninstall          remove the service, rc.local hooks, loops and files
#                        under /data (keeps /data/qw-agent.env unless --purge)
#   --purge              with --uninstall: also delete /data/qw-agent.env
#   --pylib-tarball F    use a prebuilt pylib tarball (from `make release`)
#                        instead of running pip on this workstation
#   --no-doctor          skip the qw_doctor.sh self-check at the end
#   SSH_KEY=~/.ssh/key   identity file
#
# Idempotent: re-running updates scripts/agent/service. Never overwrites an
# existing /data/qw-agent.env. When none exists and stdin is a terminal it asks
# for the four values every site needs; otherwise it seeds .env.example.
# =============================================================================
set -euo pipefail

CERBO_HOST="${CERBO_HOST:?set CERBO_HOST, e.g. root@192.168.1.232}"
SSH_KEY="${SSH_KEY:-}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

RESTART=0; DRY_WINDOW=0; UNINSTALL=0; PURGE=0; PYLIB_TARBALL=""; DOCTOR=1
while [ $# -gt 0 ]; do
  case "$1" in
    --restart) RESTART=1 ;;
    --dry-run-window) DRY_WINDOW="${2:?--dry-run-window needs seconds}"; shift ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    --pylib-tarball) PYLIB_TARBALL="${2:?--pylib-tarball needs a file}"; shift ;;
    --no-doctor) DOCTOR=0 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$SSH_KEY" ] && SSH_OPTS+=(-i "$SSH_KEY")

ssh_() { ssh "${SSH_OPTS[@]}" "$CERBO_HOST" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }

echo "==> Target: $CERBO_HOST"

# --- uninstall -------------------------------------------------------------- #
if [ "$UNINSTALL" = 1 ]; then
  echo "==> Uninstalling (purge env: $PURGE)"
  ssh_ "PURGE=$PURGE sh -s" <<'REMOTE'
set -e
if [ -e /service/qw-agent ]; then
  svc -d /service/qw-agent 2>/dev/null || true
  svc -x /service/qw-agent 2>/dev/null || true
  rm -rf /service/qw-agent
fi
# loops started from rc.local
for pat in qw_dess_watchdog qw_log_audit afrr_capture; do
  for pid in $(ps | grep -v grep | grep "$pat" | awk '{print $1}'); do kill "$pid" 2>/dev/null || true; done
done
[ -x /data/afrr_capture.sh ] && /data/afrr_capture.sh stop >/dev/null 2>&1 || true
# rc.local hooks: drop every line that mentions our files
if [ -f /data/rc.local ]; then
  grep -v -E 'qw-agent|qw_dess_watchdog|qw_log_audit|afrr_capture|QW (agent|DESS|log)|aFRR WORKMODE' /data/rc.local > /data/rc.local.new || true
  mv /data/rc.local.new /data/rc.local; chmod 755 /data/rc.local
fi
# leave the site in normal state before removing the actuators
[ -x /data/qw_grid_setpoint.sh ] && /data/qw_grid_setpoint.sh 0 >/dev/null 2>&1 || true
[ -x /data/qw_dess_toggle.sh ] && /data/qw_dess_toggle.sh on >/dev/null 2>&1 || true
rm -rf /data/qw-agent /data/qw_dess_toggle.sh /data/qw_grid_setpoint.sh /data/qw_dess_watchdog.sh \
       /data/qw_log_audit.sh /data/qw_doctor.sh /data/afrr_capture.sh \
       /data/qw_dess_saved_mode /data/qw_dess_saved_minsoc /data/qw_dess_event_minsoc \
       /data/qw_health_offset /tmp/qw_dess_off_at
[ "$PURGE" = 1 ] && rm -f /data/qw-agent.env
echo "   removed. Kept: /data/afrr-workmode.log (capture history)$([ "$PURGE" = 1 ] || echo ', /data/qw-agent.env')"
REMOTE
  exit 0
fi

# --- 1. Vendored python deps (pure-python, arch-independent) ---------------- #
BUILD_LIB="$REPO_DIR/build/pylib"
rm -rf "$BUILD_LIB"; mkdir -p "$BUILD_LIB"
if [ -n "$PYLIB_TARBALL" ]; then
  echo "==> Unpacking vendored python deps from $PYLIB_TARBALL"
  tar -xzf "$PYLIB_TARBALL" -C "$BUILD_LIB"
else
  echo "==> Building vendored python deps (pip --target)"
  python3 -m pip install --quiet --target "$BUILD_LIB" -r "$REPO_DIR/agent/requirements.txt"
fi

# --- 2. Actuator + maintenance scripts -> /data ----------------------------- #
echo "==> Installing scripts to /data"
scp_ "$REPO_DIR"/scripts/qw_dess_toggle.sh \
     "$REPO_DIR"/scripts/qw_grid_setpoint.sh \
     "$REPO_DIR"/scripts/qw_dess_watchdog.sh \
     "$REPO_DIR"/scripts/qw_log_audit.sh \
     "$REPO_DIR"/scripts/qw_doctor.sh "$CERBO_HOST":/data/
ssh_ 'chmod 750 /data/qw_dess_toggle.sh /data/qw_grid_setpoint.sh \
      /data/qw_dess_watchdog.sh /data/qw_log_audit.sh /data/qw_doctor.sh'

# --- 3. Agent + vendored libs -> /data/qw-agent ----------------------------- #
echo "==> Installing agent to /data/qw-agent"
ssh_ 'mkdir -p /data/qw-agent/pylib'
scp_ "$REPO_DIR"/agent/qw_agent.py "$REPO_DIR"/agent/mfrr_statemachine.py \
     "$REPO_DIR"/agent/actuators.py "$REPO_DIR"/agent/startup.py \
     "$REPO_DIR"/agent/requirements.txt \
     "$CERBO_HOST":/data/qw-agent/
scp_ -r "$REPO_DIR"/agent/telemetry "$CERBO_HOST":/data/qw-agent/
scp_ -r "$BUILD_LIB"/. "$CERBO_HOST":/data/qw-agent/pylib/

# Version stamp: agent version + git SHA + date. The agent logs it at start.
AGENT_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_DIR/agent/qw_agent.py")"
GIT_SHA="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo nogit)"
GIT_DIRTY="$(git -C "$REPO_DIR" diff --quiet 2>/dev/null || echo '-dirty')"
VERSION_LINE="${AGENT_VERSION:-?}+${GIT_SHA}${GIT_DIRTY} $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
ssh_ "printf '%s\n' '$VERSION_LINE' > /data/qw-agent/VERSION"
echo "    VERSION: $VERSION_LINE"

# --- 3b. Read-only diagnostics (aFRR verification) -------------------------- #
echo "==> Installing read-only diagnostics (afrr_probe.py, afrr_capture.sh)"
scp_ "$REPO_DIR"/tools/afrr_probe.py "$CERBO_HOST":/data/qw-agent/afrr_probe.py
scp_ "$REPO_DIR"/tools/afrr_capture.sh "$CERBO_HOST":/data/afrr_capture.sh
ssh_ 'chmod 755 /data/qw-agent/afrr_probe.py /data/afrr_capture.sh'

# --- 4. Per-site config (never overwrite existing secrets) ------------------ #
if ssh_ 'test -f /data/qw-agent.env'; then
  echo "==> /data/qw-agent.env exists — leaving it untouched"
elif [ -t 0 ]; then
  echo "==> No /data/qw-agent.env yet — a few questions (everything else keeps the documented defaults)"
  read -r -p "    Qilowatt inverter id (UUID, QW_DEVICE_ID): " Q_ID
  read -r -p "    Qilowatt MQTT user: " Q_USER
  read -r -s -p "    Qilowatt MQTT password: " Q_PASS; echo
  read -r -p "    Max grid IMPORT the connection allows, W [15000]: " Q_IMP; Q_IMP="${Q_IMP:-15000}"
  read -r -p "    Max grid EXPORT the connection allows, W [15000]: " Q_EXP; Q_EXP="${Q_EXP:-15000}"
  TMP_ENV="$(mktemp)"
  # Take the commented example, then override the site-specific keys.
  sed -e "s|^QW_DEVICE_ID=.*|QW_DEVICE_ID=$Q_ID|" \
      -e "s|^QW_MQTT_USER=.*|QW_MQTT_USER=$Q_USER|" \
      -e "s|^QW_MQTT_PASS=.*|QW_MQTT_PASS=$Q_PASS|" \
      -e "s|^QW_MAX_IMPORT_W=.*|QW_MAX_IMPORT_W=$Q_IMP|" \
      -e "s|^QW_MAX_EXPORT_W=.*|QW_MAX_EXPORT_W=$Q_EXP|" \
      "$REPO_DIR/.env.example" > "$TMP_ENV"
  scp_ "$TMP_ENV" "$CERBO_HOST":/data/qw-agent.env
  rm -f "$TMP_ENV"
  ssh_ 'chmod 600 /data/qw-agent.env'
  echo "    wrote /data/qw-agent.env (chmod 600). Review QW_MFRR_MIN_SOC and QW_MFRR_SOURCES there."
else
  echo "==> Seeding /data/qw-agent.env from .env.example (EDIT IT with real creds)"
  scp_ "$REPO_DIR"/.env.example "$CERBO_HOST":/data/qw-agent.env
  ssh_ 'chmod 600 /data/qw-agent.env'
fi

# --- 5. daemontools service (persistent under /data, linked into /service) -- #
# /service lives on the rootfs and is WIPED by Venus OS firmware updates. Keep
# the service definition under /data (persistent) and symlink it into /service.
# The symlink is recreated on every boot from /data/rc.local (step 6), so the
# agent — and thus Qilowatt telemetry — survives firmware updates.
echo "==> Installing agent service under /data/qw-agent/service + linking into /service"
ssh_ 'mkdir -p /data/qw-agent/service'
scp_ -r "$REPO_DIR"/service/qw-agent "$CERBO_HOST":/data/qw-agent/service/
ssh_ 'mkdir -p /var/log/qw-agent
chmod 755 /data/qw-agent/service/qw-agent/run /data/qw-agent/service/qw-agent/log/run
if [ "$(readlink /service/qw-agent 2>/dev/null)" != /data/qw-agent/service/qw-agent ]; then
  rm -rf /service/qw-agent
  ln -s /data/qw-agent/service/qw-agent /service/qw-agent
fi'

# --- 6. Boot hooks in /data/rc.local (survive firmware updates) ------------- #
# Idempotent blocks: (a) re-link the agent service into /service (rootfs is
# wiped on firmware update), (b) the DESS watchdog loop, (c) the WORKMODE
# capture, (d) the hourly log audit. Each loop is also started now if absent.
echo "==> Ensuring boot hooks in /data/rc.local"
ssh_ 'sh -s' <<'REMOTE'
set -e
RC=/data/rc.local
[ -f "$RC" ] || { printf '#!/bin/sh\n' > "$RC"; chmod 755 "$RC"; }

# (a) agent service relink — recreate /service/qw-agent from the /data copy
if ! grep -q 'qw-agent/service/qw-agent' "$RC"; then
  cat >> "$RC" <<'EOF'

# QW agent service — /service and /var/log are on the rootfs/tmpfs and are
# WIPED by Venus OS firmware updates (and /var/log on every boot). The service
# definition lives under /data (persistent); recreate the log dir and re-link
# the service into /service on every boot. svscan picks it up within ~5 s.
mkdir -p /var/log/qw-agent
if [ "$(readlink /service/qw-agent 2>/dev/null)" != /data/qw-agent/service/qw-agent ]; then
  rm -rf /service/qw-agent
  ln -s /data/qw-agent/service/qw-agent /service/qw-agent
fi
EOF
  echo "   added agent service relink"
else
  echo "   agent service relink already present"
fi

# (b) DESS watchdog loop. `sh -c` rather than `( ... ) &` so the loop keeps the
#     script name in its cmdline — a subshell inherits rc.local's name instead,
#     which makes the liveness check below silently useless and spawns a
#     duplicate loop on every re-run.
if ! grep -q qw_dess_watchdog "$RC"; then
  cat >> "$RC" <<'EOF'

# QW DESS watchdog
nohup sh -c 'while true; do /data/qw_dess_watchdog.sh; sleep 60; done' >/dev/null 2>&1 &
EOF
  echo "   added watchdog loop"
else
  echo "   watchdog loop already present"
fi
if ! ps | grep -v grep | grep -q qw_dess_watchdog; then
  nohup sh -c 'while true; do /data/qw_dess_watchdog.sh; sleep 60; done' >/dev/null 2>&1 &
  echo "   started watchdog loop"
fi

# (c) durable WORKMODE capture — the log ring is too short to size the
#     idle-refresh watchdog on a site that gets few commands
if ! grep -q 'afrr_capture.sh start' "$RC"; then
  cat >> "$RC" <<'EOF'

# aFRR WORKMODE capture (verification) — durable, read-only tap
[ -x /data/afrr_capture.sh ] && /data/afrr_capture.sh start
EOF
  echo "   added WORKMODE capture autostart"
else
  echo "   WORKMODE capture autostart already present"
fi
if [ -x /data/afrr_capture.sh ]; then
  /data/afrr_capture.sh start >/dev/null 2>&1 || true
fi

# (d) hourly log audit — every defect so far was visible in the log and missed
if ! grep -q qw_log_audit "$RC"; then
  cat >> "$RC" <<'EOF'

# QW log audit — reports new FAILSAFE / crash / watchdog / SOC-floor symptoms
nohup sh -c 'while true; do /data/qw_log_audit.sh >/dev/null 2>&1; sleep 3600; done' >/dev/null 2>&1 &
EOF
  echo "   added log audit loop"
else
  echo "   log audit loop already present"
fi
if ! ps | grep -v grep | grep -q qw_log_audit; then
  nohup sh -c 'while true; do /data/qw_log_audit.sh >/dev/null 2>&1; sleep 3600; done' >/dev/null 2>&1 &
  echo "   started log audit loop"
fi
REMOTE

# --- 7. Optional dry-run window -------------------------------------------- #
# Stops the supervised agent, runs the new code once with QW_DRY_RUN=1 for N
# seconds (nothing is actuated; intentions are logged), then restores the
# service. BusyBox has no `timeout`, so the kill is pid-based.
if [ "$DRY_WINDOW" -gt 0 ]; then
  echo "==> Dry-run window: ${DRY_WINDOW}s (service stopped meanwhile)"
  ssh_ "N=$DRY_WINDOW sh -s" <<'REMOTE'
set -e
svc -d /service/qw-agent
sleep 2
cd /data/qw-agent
QW_DRY_RUN=1 QW_AGENT_ENV=/data/qw-agent.env PYTHONPATH=/data/qw-agent/pylib \
  python3 qw_agent.py > /tmp/qw_dryrun.log 2>&1 &
PID=$!
sleep "$N"
kill -TERM "$PID" 2>/dev/null || true
sleep 3
kill -KILL "$PID" 2>/dev/null || true
svc -u /service/qw-agent
echo "----- dry-run log (/tmp/qw_dryrun.log) -----"
cat /tmp/qw_dryrun.log
echo "----- end of dry-run log; service restarted -----"
REMOTE
  RESTART=0   # the service was just brought back up
fi

# --- 8. Restart ------------------------------------------------------------ #
if [ "$RESTART" = 1 ]; then
  echo "==> Restarting agent service"
  ssh_ 'svc -d /service/qw-agent; sleep 2; svc -u /service/qw-agent; sleep 3; svstat /service/qw-agent'
fi

# --- 9. Doctor ------------------------------------------------------------- #
if [ "$DOCTOR" = 1 ]; then
  echo "==> Self-check (/data/qw_doctor.sh)"
  ssh_ '/data/qw_doctor.sh' || echo "    doctor reported FAIL lines — fix them before going live"
fi

echo
echo "==> Done. The service lives under /data and is re-linked into /service on"
echo "    every boot, so it survives Venus OS firmware updates."
if [ "$RESTART" = 0 ] && [ "$DRY_WINDOW" = 0 ]; then
  echo "    The RUNNING agent still has the previous code — restart when ready:"
  echo "      ssh $CERBO_HOST 'svc -d /service/qw-agent; svc -u /service/qw-agent'"
  echo "    or re-run with --restart / --dry-run-window 120."
fi
echo "    Watch: ssh $CERBO_HOST tail -F /var/log/qw-agent/current"
