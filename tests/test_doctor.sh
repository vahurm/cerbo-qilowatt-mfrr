#!/bin/sh
# =============================================================================
# test_doctor.sh — POSIX-sh tests for scripts/qw_doctor.sh
# =============================================================================
# `dbus`, `svstat` and `hostname` are stubbed via PATH; python/ps/time checks
# are skipped (QW_DOCTOR_SKIP) because they describe the test host, not a
# Cerbo. Everything else — env-file parsing, dbus-path checks, ESS/DESS state,
# cap consistency, leftover state, service link, log scan — is exercised.
#
#   sh tests/test_doctor.sh   # exit 0 = all passed
# =============================================================================

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
TARGET="$REPO/scripts/qw_doctor.sh"
[ -f "$TARGET" ] || { echo "FATAL: $TARGET missing" >&2; exit 99; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

mkdir -p "$TMP/dbusstate"
cat > "$TMP/dbus" <<'EOF'
#!/bin/sh
# `dbus -y` alone lists services; otherwise -y SERVICE PATH GetValue
if [ $# -le 1 ]; then cat "$DBUS_STATE/_services" 2>/dev/null; exit 0; fi
svc="$2"; path="$3"; op="$4"
key=$(printf '%s%s' "$svc" "$path" | tr '/.' '__')
f="$DBUS_STATE/$key"
case "$op" in
  GetValue) [ -f "$f" ] && printf 'value = %s\n' "$(cat "$f")" ;;
esac
exit 0
EOF
cat > "$TMP/svstat" <<'EOF'
#!/bin/sh
cat "$DBUS_STATE/_svstat" 2>/dev/null || echo "$1: down 0 seconds"
EOF
printf '#!/bin/sh\necho cerbo-test\n' > "$TMP/hostname"
chmod +x "$TMP/dbus" "$TMP/svstat" "$TMP/hostname"
DBUS_STATE="$TMP/dbusstate"; export DBUS_STATE
PATH="$TMP:$PATH"; export PATH

set_dbus() { printf '%s' "$3" > "$DBUS_STATE/$(printf '%s%s' "$1" "$2" | tr '/.' '__')"; }
SET=com.victronenergy.settings
SYS=com.victronenergy.system

pass=0; fail=0
assert_contains() {
  desc=$1; needle=$2; hay=$3
  case "$hay" in
    *"$needle"*) pass=$((pass + 1)); echo "ok   - $desc" ;;
    *) fail=$((fail + 1)); echo "FAIL - $desc (no '$needle' in output)"; printf '%s\n' "$hay" | sed 's/^/       | /' ;;
  esac
}
assert_missing() {
  desc=$1; needle=$2; hay=$3
  case "$hay" in
    *"$needle"*) fail=$((fail + 1)); echo "FAIL - $desc (unexpected '$needle')" ;;
    *) pass=$((pass + 1)); echo "ok   - $desc" ;;
  esac
}
assert_eq() {
  desc=$1; want=$2; got=$3
  if [ "$got" = "$want" ]; then pass=$((pass + 1)); echo "ok   - $desc (= $got)"
  else fail=$((fail + 1)); echo "FAIL - $desc (want '$want', got '$got')"; fi
}

SITE="$TMP/site"
reset() {
  rm -rf "$SITE" "$DBUS_STATE"; mkdir -p "$SITE/agent/service/qw-agent" "$SITE/state" "$SITE/log" "$DBUS_STATE"
  ln -s "$SITE/agent/service/qw-agent" "$SITE/service-link"
  printf '%s: up (pid 1234) 100 seconds\n' "$SITE/service-link" > "$DBUS_STATE/_svstat"
  printf 'qw_dess_watchdog\nqw_log_audit\nafrr_capture.sh start\nqw-agent/service/qw-agent\n' > "$SITE/rc.local"
  # healthy dbus
  set_dbus $SET /Settings/CGwacs/AcPowerSetPoint 0
  set_dbus $SET /Settings/CGwacs/BatteryLife/MinimumSocLimit 30
  set_dbus $SET /Settings/CGwacs/BatteryLife/State 10
  set_dbus $SET /Settings/CGwacs/MaxFeedInPower 15000
  set_dbus $SET /Settings/DynamicEss/Mode 1
  set_dbus $SYS /Dc/Battery/Soc 55
  set_dbus $SYS /Ac/Grid/L1/Power 120
  # healthy env
  cat > "$SITE/qw-agent.env" <<'EOF'
QW_DEVICE_ID=12345678-1234-1234-1234-123456789abc
QW_MQTT_USER=u
QW_MQTT_PASS=p
QW_MAX_IMPORT_W=28000
QW_MAX_EXPORT_W=15000
QW_MAX_EVENT_S=7200
QW_MAX_TRADE_S=5400
QW_MFRR_MIN_SOC=20
QW_MFRR_SOURCES=fusebox,kratt,qilowatt
EOF
  chmod 600 "$SITE/qw-agent.env"
}

run() {
  env QW_AGENT_ENV="$SITE/qw-agent.env" QW_AGENT_DIR="$SITE/agent" QW_STATE_DIR="$SITE/state" \
      QW_SERVICE_LINK="$SITE/service-link" QW_RC_LOCAL="$SITE/rc.local" QW_LOG_DIR="$SITE/log" \
      QW_OFF_AT_FILE="$SITE/state/off_at" QW_DOCTOR_SKIP=python,ps,time "$@" sh "$TARGET" 2>&1
}
run_rc() { run "$@" >/dev/null 2>&1; echo $?; }

echo "=== 1: healthy site passes with no FAIL ==="
reset
out=$(run)
assert_contains "no FAIL lines" ", 0 FAIL" "$out"
assert_contains "DESS mode pass" "PASS  DESS Mode = 1" "$out"
assert_contains "caps pass" "PASS  caps import=28000 W export=15000 W" "$out"
assert_contains "floor pass" "PASS  QW_MFRR_MIN_SOC=20 below live ESS floor 30" "$out"
assert_contains "service link pass" "PASS  $SITE/service-link ->" "$out"
assert_contains "service up" "PASS  service" "$out"
assert_eq "exit 0" "0" "$(run_rc)"

echo "=== 2: placeholders and missing env file FAIL ==="
reset
printf 'QW_DEVICE_ID=REPLACE_WITH_INVERTER_ID\nQW_MQTT_USER=u\nQW_MQTT_PASS=p\n' > "$SITE/qw-agent.env"
out=$(run)
assert_contains "placeholder FAIL" "FAIL  $SITE/qw-agent.env still has REPLACE_WITH_* placeholders: REPLACE_WITH_INVERTER_ID" "$out"
assert_contains "missing caps WARN" "WARN  QW_MAX_IMPORT_W / QW_MAX_EXPORT_W not set" "$out"
assert_eq "exit 1" "1" "$(run_rc)"
rm "$SITE/qw-agent.env"
out=$(run)
assert_contains "missing env FAIL" "FAIL  $SITE/qw-agent.env missing" "$out"

echo "=== 3: env file permissions and device id format ==="
reset
chmod 644 "$SITE/qw-agent.env"
sed -i 's/^QW_DEVICE_ID=.*/QW_DEVICE_ID=abcdef0123/' "$SITE/qw-agent.env"
out=$(run)
assert_contains "mode WARN" "WARN  $SITE/qw-agent.env mode 644" "$out"
assert_contains "uuid WARN" "is not a UUID" "$out"

echo "=== 4: DESS absent / off, ESS keep-charged ==="
reset
rm "$DBUS_STATE/$(printf '%s%s' $SET /Settings/DynamicEss/Mode | tr '/.' '__')"
out=$(run)
assert_contains "DESS absent FAIL" "FAIL  /Settings/DynamicEss/Mode unreadable" "$out"
reset
set_dbus $SET /Settings/DynamicEss/Mode 0
out=$(run)
assert_contains "DESS off WARN" "WARN  DESS Mode = 0 (off) — the agent will restore" "$out"
echo 1 > "$SITE/state/qw_dess_saved_mode"
out=$(run)
assert_contains "DESS off with saved mode WARN" "with a saved Mode on disk" "$out"
reset
set_dbus $SET /Settings/CGwacs/BatteryLife/State 9
out=$(run)
assert_contains "keep charged FAIL" "FAIL  ESS is in 'Keep batteries charged'" "$out"

echo "=== 5: missing SOC is FAIL, missing grid meter is WARN ==="
reset
rm "$DBUS_STATE/$(printf '%s%s' $SYS /Dc/Battery/Soc | tr '/.' '__')"
out=$(run)
assert_contains "SOC FAIL" "FAIL  $SYS /Dc/Battery/Soc unreadable" "$out"
reset
rm "$DBUS_STATE/$(printf '%s%s' $SYS /Ac/Grid/L1/Power | tr '/.' '__')"
out=$(run)
assert_contains "grid WARN" "WARN  $SYS /Ac/Grid/L1/Power unreadable" "$out"
assert_eq "exit 0 with only WARN" "0" "$(run_rc)"

echo "=== 6: cap consistency ==="
reset
sed -i 's/^QW_MAX_EXPORT_W=.*/QW_MAX_EXPORT_W=20000/' "$SITE/qw-agent.env"
out=$(run)
assert_contains "export > MaxFeedInPower WARN" "WARN  QW_MAX_EXPORT_W=20000 exceeds ESS MaxFeedInPower=15000" "$out"
reset
sed -i 's/^QW_MAX_EVENT_S=.*/QW_MAX_EVENT_S=7800/' "$SITE/qw-agent.env"
out=$(run)
assert_contains "event cap FAIL" "FAIL  QW_MAX_EVENT_S=7800 is not below the DESS watchdog cap QW_MAX_OFF_SECS=7800" "$out"
reset
printf 'QW_MAX_OFF_SECS=9000\n' >> "$SITE/qw-agent.env"
out=$(run)
assert_contains "env watchdog cap honoured" "PASS  QW_MAX_EVENT_S=7200 < watchdog QW_MAX_OFF_SECS=9000" "$out"
reset
sed -i 's/^QW_MAX_IMPORT_W=.*/QW_MAX_IMPORT_W=15000/' "$SITE/qw-agent.env"
out=$(run)
assert_contains "example caps WARN" "are the example 15000" "$out"

echo "=== 7: import suggestion from AC input current limit ==="
reset
printf 'com.victronenergy.vebus.ttyS4\n' > "$DBUS_STATE/_services"
set_dbus com.victronenergy.vebus.ttyS4 /Ac/ActiveIn/CurrentLimit 32
set_dbus com.victronenergy.vebus.ttyS4 /Ac/NumberOfPhases 3
out=$(run)
assert_contains "suggestion printed" "info  AC input current limit 32 A x 3 ph -> ~22080 W" "$out"
assert_contains "import above limit WARN" "WARN  QW_MAX_IMPORT_W=28000 is above the AC input limit (~22080 W)" "$out"

echo "=== 8: SOC floor not below live floor, sources without qilowatt, dry run ==="
reset
sed -i 's/^QW_MFRR_MIN_SOC=.*/QW_MFRR_MIN_SOC=30/' "$SITE/qw-agent.env"
sed -i 's/^QW_MFRR_SOURCES=.*/QW_MFRR_SOURCES=fusebox,kratt/' "$SITE/qw-agent.env"
printf 'QW_DRY_RUN=1\n' >> "$SITE/qw-agent.env"
out=$(run)
assert_contains "floor WARN" "WARN  QW_MFRR_MIN_SOC=30 is NOT below the live ESS floor 30" "$out"
assert_contains "sources WARN" "excludes 'qilowatt'" "$out"
assert_contains "dry run WARN" "WARN  QW_DRY_RUN=1" "$out"

echo "=== 9: leftover state and state.json ==="
reset
echo 1 > "$SITE/state/qw_dess_saved_mode"
printf '{"state": "IDLE", "kind": null, "degraded": false}' > "$SITE/agent/state.json"
out=$(run)
assert_contains "leftover WARN" "WARN  agent says IDLE but a saved DESS Mode exists" "$out"
reset
printf '{"state": "ACTIVE", "kind": "frr", "degraded": true}' > "$SITE/agent/state.json"
out=$(run)
assert_contains "degraded WARN" "WARN  state.json reports degraded=true" "$out"

echo "=== 10: service link / rc.local / svstat ==="
reset
rm "$SITE/service-link"
out=$(run)
assert_contains "link missing FAIL" "FAIL  $SITE/service-link missing" "$out"
reset
rm "$SITE/service-link"; ln -s /service/elsewhere "$SITE/service-link"
out=$(run)
assert_contains "wrong link WARN" "WARN  $SITE/service-link -> /service/elsewhere" "$out"
reset
printf '%s: down 12 seconds, normally up\n' "$SITE/service-link" > "$DBUS_STATE/_svstat"
out=$(run)
assert_contains "service down FAIL" "FAIL  service not up" "$out"
reset
printf 'qw_log_audit\n' > "$SITE/rc.local"
out=$(run)
assert_contains "missing hook WARN" "lacks the 'qw_dess_watchdog' hook" "$out"
assert_missing "present hook quiet" "lacks the 'qw_log_audit' hook" "$out"

echo "=== 11: recent log scan ==="
reset
{
  echo "2026-09-14 10:00:00 INFO qw_agent: Starting qw_agent 1.0.0"
  echo "2026-09-14 10:00:05 ERROR qw_agent.mfrr: actuation failed: setpoint 3000 W (after retry) -> DEGRADED"
  echo "2026-09-14 10:01:00 ERROR qw_agent: telemetry unavailable (dbus not available); SENSOR not published (1 skipped)"
} > "$SITE/log/current"
out=$(run)
assert_contains "actuation failed WARN" "WARN  1 x 'actuation failed'" "$out"
assert_contains "telemetry WARN" "WARN  1 x 'telemetry unavailable'" "$out"
assert_contains "last start" "info  last start: 2026-09-14 10:00:00 INFO qw_agent: Starting qw_agent 1.0.0" "$out"

echo "=== 12: VERSION stamp is printed ==="
reset
echo "1.0.0+abc1234 2026-09-14" > "$SITE/agent/VERSION"
out=$(run)
assert_contains "version" "agent install: 1.0.0+abc1234 2026-09-14" "$out"

echo "-----------------------------------------"
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
