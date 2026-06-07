#!/usr/bin/env bash
# =============================================================================
# install.sh — deploy cerbo-qilowatt-mfrr to a Cerbo GX over SSH
# =============================================================================
# Run from a workstation (NOT on the Cerbo). Pure-Python deps are vendored
# locally and copied over, so the Cerbo needs no pip/internet. dbus access uses
# the Cerbo's system python3-dbus.
#
# Usage:
#   CERBO_HOST=root@192.168.1.232 ./deploy/install.sh
#   CERBO_HOST=root@192.168.1.232 SSH_KEY=~/.ssh/cerbo ./deploy/install.sh
#
# Idempotent: re-running updates scripts/agent/service. Never overwrites an
# existing /data/qw-agent.env (your secrets).
# =============================================================================
set -euo pipefail

CERBO_HOST="${CERBO_HOST:?set CERBO_HOST, e.g. root@192.168.1.232}"
SSH_KEY="${SSH_KEY:-}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$SSH_KEY" ] && SSH_OPTS+=(-i "$SSH_KEY")

ssh_() { ssh "${SSH_OPTS[@]}" "$CERBO_HOST" "$@"; }
scp_() { scp "${SSH_OPTS[@]}" "$@"; }

echo "==> Target: $CERBO_HOST"

# --- 1. Build vendored python deps locally (pure-python, arch-independent) ---
echo "==> Building vendored python deps"
BUILD_LIB="$REPO_DIR/build/pylib"
rm -rf "$BUILD_LIB"; mkdir -p "$BUILD_LIB"
python3 -m pip install --quiet --target "$BUILD_LIB" -r "$REPO_DIR/agent/requirements.txt"

# --- 2. Actuator scripts -> /data ------------------------------------------
echo "==> Installing actuator scripts to /data"
scp_ "$REPO_DIR"/scripts/qw_dess_toggle.sh \
     "$REPO_DIR"/scripts/qw_grid_setpoint.sh \
     "$REPO_DIR"/scripts/qw_dess_watchdog.sh "$CERBO_HOST":/data/
ssh_ 'chmod 750 /data/qw_dess_toggle.sh /data/qw_grid_setpoint.sh /data/qw_dess_watchdog.sh'

# --- 3. Agent + vendored libs -> /data/qw-agent ----------------------------
echo "==> Installing agent to /data/qw-agent"
ssh_ 'mkdir -p /data/qw-agent/pylib'
scp_ "$REPO_DIR"/agent/qw_agent.py "$REPO_DIR"/agent/mfrr_statemachine.py \
     "$REPO_DIR"/agent/actuators.py "$REPO_DIR"/agent/requirements.txt \
     "$CERBO_HOST":/data/qw-agent/
scp_ -r "$REPO_DIR"/agent/telemetry "$CERBO_HOST":/data/qw-agent/
scp_ -r "$BUILD_LIB"/. "$CERBO_HOST":/data/qw-agent/pylib/

# --- 4. Per-site config (never overwrite existing secrets) ------------------
if ssh_ 'test -f /data/qw-agent.env'; then
  echo "==> /data/qw-agent.env exists — leaving it untouched"
else
  echo "==> Seeding /data/qw-agent.env from .env.example (EDIT IT with real creds)"
  scp_ "$REPO_DIR"/.env.example "$CERBO_HOST":/data/qw-agent.env
  ssh_ 'chmod 600 /data/qw-agent.env'
fi

# --- 5. daemontools service (persistent under /data, linked into /service) --
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

# --- 6. Boot hooks in /data/rc.local (survive firmware updates) -------------
# Two idempotent blocks: (a) re-link the agent service into /service (rootfs is
# wiped on firmware update), and (b) the DESS watchdog loop.
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

# (b) DESS watchdog loop
if ! grep -q qw_dess_watchdog "$RC"; then
  cat >> "$RC" <<'EOF'

# QW DESS watchdog
( while true; do /data/qw_dess_watchdog.sh; sleep 60; done ) &
EOF
  echo "   added watchdog loop"
else
  echo "   watchdog loop already present"
fi
# start the watchdog now if not already running
if ! ps | grep -v grep | grep -q qw_dess_watchdog; then
  nohup sh -c 'while true; do /data/qw_dess_watchdog.sh; sleep 60; done' >/dev/null 2>&1 &
  echo "   started watchdog loop"
fi
REMOTE

echo
echo "==> Done. The service lives under /data and is re-linked into /service on"
echo "    every boot, so it survives Venus OS firmware updates."
echo "    Next steps:"
echo "    1) Edit /data/qw-agent.env on the Cerbo with the real Qilowatt creds"
echo "    2) svc -d /service/qw-agent ; svc -u /service/qw-agent   # restart agent"
echo "    3) tail -F /var/log/qw-agent/current"
echo "    4) mosquitto_sub -h 127.0.0.1 -t 'qw/#' -v                # decoded WORKMODE"
echo "    5) Import nodered/flow.json into Node-RED (stays disabled until you enable)"
