#!/bin/sh
# =============================================================================
# test_log_audit.sh — POSIX-sh tests for scripts/qw_log_audit.sh
# =============================================================================
# `logger` is stubbed via PATH. The agent log is a plain file we append to, so
# the incremental "only new lines" behaviour can be driven directly.
#
#   sh tests/test_log_audit.sh   # exit 0 = all passed
# =============================================================================

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
TARGET="$REPO/scripts/qw_log_audit.sh"

if [ ! -f "$TARGET" ]; then
  echo "FATAL: target not found: $TARGET" >&2
  exit 99
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

cat > "$TMP/logger" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$TMP/logger"
PATH="$TMP:$PATH"
export PATH

pass=0
fail=0
assert_eq() {
  desc=$1; want=$2; got=$3
  if [ "$got" = "$want" ]; then
    pass=$((pass + 1)); echo "ok   - $desc (= $got)"
  else
    fail=$((fail + 1)); echo "FAIL - $desc (want '$want', got '$got')"
  fi
}
assert_contains() {
  desc=$1; needle=$2; hay=$3
  case "$hay" in
    *"$needle"*) pass=$((pass + 1)); echo "ok   - $desc" ;;
    *) fail=$((fail + 1)); echo "FAIL - $desc (no '$needle' in: $hay)" ;;
  esac
}
assert_missing() {
  desc=$1; needle=$2; hay=$3
  case "$hay" in
    *"$needle"*) fail=$((fail + 1)); echo "FAIL - $desc (unexpected '$needle')" ;;
    *) pass=$((pass + 1)); echo "ok   - $desc" ;;
  esac
}

LOGDIR="$TMP/log"
STATE="$TMP/state"
CAPTURE="$TMP/capture.log"

reset() {
  rm -rf "$LOGDIR" "$STATE"
  mkdir -p "$LOGDIR" "$STATE"
  : > "$LOGDIR/current"
  : > "$CAPTURE"          # mtime = now, i.e. a command just arrived
}

run() {
  env QW_LOG_DIR="$LOGDIR" QW_STATE_DIR="$STATE" QW_CAPTURE_LOG="$CAPTURE" \
      "$@" sh "$TARGET" 2>&1
}
# run_rc <extra env...> -> prints exit code only
run_rc() {
  env QW_LOG_DIR="$LOGDIR" QW_STATE_DIR="$STATE" QW_CAPTURE_LOG="$CAPTURE" \
      "$@" sh "$TARGET" >/dev/null 2>&1
  echo $?
}

# age_capture <hours> — pretend the last captured command arrived that long ago
age_capture() {
  touch -d "@$(( $(date +%s) - $1 * 3600 ))" "$CAPTURE"
}

say() { printf '%s\n' "$1" >> "$LOGDIR/current"; }

echo "=== scenario 1: healthy log is quiet and exits 0 ==="
reset
say "2026-07-26 10:00:00 INFO qw_agent: Starting qw_agent device=x"
say "2026-07-26 10:00:01 INFO qw_agent: WORKMODE received: {'Mode': 'normal'}"
out=$(run)
assert_contains "reports OK" "OK" "$out"
reset
say "2026-07-26 10:00:00 INFO qw_agent: WORKMODE received: {'Mode': 'normal'}"
assert_eq "exit 0 when healthy" "0" "$(run_rc)"

echo "=== scenario 2: a traceback is an ERROR and exits 1 ==="
reset
say "2026-07-26 10:00:00 ERROR qw_agent: boom"
say "Traceback (most recent call last):"
out=$(run)
assert_contains "traceback reported" "ERROR" "$out"
reset
say "Traceback (most recent call last):"
assert_eq "exit 1 on traceback" "1" "$(run_rc)"

echo "=== scenario 3: findings are reported once, not on every run ==="
reset
say "Traceback (most recent call last):"
first=$(run)
assert_contains "first run sees it" "Traceback" "$first"
second=$(run)
assert_contains "second run has nothing new" "no new log lines" "$second"
assert_missing "old finding not repeated" "ERROR" "$second"

echo "=== scenario 4: only lines added since the last run are examined ==="
reset
say "Traceback (most recent call last):"
run >/dev/null
say "2026-07-26 10:05:00 WARNING qw_dess: SOC floor already 15% (<= mFRR floor 20%); nothing to lower"
out=$(run)
assert_contains "new SOC-floor finding seen" "two-level SOC floor did not engage" "$out"
assert_missing "old traceback not re-reported" "agent crashed" "$out"

echo "=== scenario 5: log rotation resets progress instead of skipping ==="
reset
say "2026-07-26 10:00:00 INFO qw_agent: line one"
say "2026-07-26 10:00:01 INFO qw_agent: line two"
say "2026-07-26 10:00:02 INFO qw_agent: line three"
run >/dev/null
: > "$LOGDIR/current"          # multilog rotated: current starts over
say "Traceback (most recent call last):"
out=$(run)
assert_contains "post-rotation line is examined" "agent crashed" "$out"

echo "=== scenario 6: restart loop warns above the threshold ==="
reset
i=0
while [ "$i" -lt 5 ]; do
  say "2026-07-26 10:0${i}:00 INFO qw_agent: Starting qw_agent device=x"
  i=$((i + 1))
done
out=$(run)
assert_contains "restart loop reported" "restarted more than" "$out"

echo "=== scenario 7: idle-refresh and mode-gate findings are recognised ==="
reset
say "2026-07-26 10:00:00 ERROR qw_agent: no WORKMODE command received for 21601s (>= 21600s) while IDLE"
say "2026-07-26 10:00:01 INFO qw_agent.mfrr: ignoring non-FRR Mode 'buy' from mFRR source 'qilowatt' (PowerLimit=5000)"
out=$(run)
assert_contains "idle-refresh reported" "idle-refresh restarted the agent" "$out"
assert_contains "mode gate reported" "dropped by the mode gate" "$out"

echo "=== scenario 7b: a rejected setpoint is an ERROR ==="
# The clamp refusing a setpoint means that dispatch was not delivered at all.
reset
say "2026-07-26 10:00:00 INFO qw_agent.actuators: actuator ['/data/qw_grid_setpoint.sh', '25000'] -> REJECT: 25000 W (import over 15000)"
out=$(run)
assert_contains "rejection reported" "dispatch was NOT delivered" "$out"
reset
say "2026-07-26 10:00:00 INFO qw_agent.actuators: actuator -> REJECT: 25000 W (import over 15000)"
assert_eq "exit 1 on rejection" "1" "$(run_rc)"

echo "=== scenario 8: a missing log is not an error ==="
reset
rm -f "$LOGDIR/current"
out=$(run)
assert_contains "missing log explained" "no agent log" "$out"
assert_eq "exit 0 when log absent" "0" "$(run_rc)"

echo "=== scenario 9: a recent command means no silence warning ==="
reset
say "2026-07-26 10:00:00 INFO qw_agent: WORKMODE received: {'Mode': 'normal'}"
out=$(run)
assert_missing "no silence warning" "no WORKMODE command for" "$out"

echo "=== scenario 10: silence is reported even with nothing new in the log ==="
# The case that matters: a deaf site logs nothing at all, so a check that needs
# new log lines would never fire. This is also the only detection left on a site
# where QW_IDLE_REFRESH_S is 0.
reset
run >/dev/null                 # consume the baseline
age_capture 40
out=$(run)
assert_contains "no new log lines noted" "no new log lines" "$out"
assert_contains "silence still reported" "no WORKMODE command for 40h" "$out"
assert_eq "exit 1 on silence" "1" "$(run_rc)"

echo "=== scenario 11: silence below the threshold stays quiet ==="
reset
age_capture 12
out=$(run)
assert_missing "12h is not yet a finding" "no WORKMODE command for" "$out"

echo "=== scenario 12: silence check can be disabled ==="
reset
age_capture 99
out=$(run QW_HEALTH_MAX_SILENCE_H=0)
assert_missing "disabled by QW_HEALTH_MAX_SILENCE_H=0" "no WORKMODE command for" "$out"

echo "-----------------------------------------"
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
