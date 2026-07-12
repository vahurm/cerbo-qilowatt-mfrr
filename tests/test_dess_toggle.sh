#!/bin/sh
# =============================================================================
# test_dess_toggle.sh — POSIX-sh tests for scripts/qw_dess_toggle.sh
# =============================================================================
# No bats dependency. `dbus` and `logger` are stubbed via PATH. The `dbus` stub
# is STATEFUL (stores each path's value in a file), so we can assert the full
# save / lower / restore lifecycle of both DESS Mode and the SOC floor.
#
#   sh tests/test_dess_toggle.sh   # exit 0 = all passed
# =============================================================================

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
TARGET="$REPO/scripts/qw_dess_toggle.sh"

if [ ! -f "$TARGET" ]; then
  echo "FATAL: target not found: $TARGET" >&2
  exit 99
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

MODE_PATH="/Settings/DynamicEss/Mode"
MINSOC_PATH="/Settings/CGwacs/BatteryLife/MinimumSocLimit"

# --- PATH stubs ------------------------------------------------------------ #
# Stateful dbus: args are `-y SERVICE PATH OP [%VAL]`. GetValue prints stored
# value (as "value = N"); SetValue stores the numeric value for that PATH.
mkdir -p "$TMP/dbusstate"
cat > "$TMP/dbus" <<'EOF'
#!/bin/sh
path="$3"; op="$4"
key=$(printf '%s' "$path" | tr '/' '_')
f="$DBUS_STATE/$key"
case "$op" in
  GetValue) [ -f "$f" ] && printf 'value = %s\n' "$(cat "$f")" ;;
  SetValue) v="$5"; v="${v#%}"; printf '%s' "$v" > "$f" ;;
esac
exit 0
EOF
cat > "$TMP/logger" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$TMP/dbus" "$TMP/logger"

DBUS_STATE="$TMP/dbusstate"
export DBUS_STATE
PATH="$TMP:$PATH"
export PATH

# state accessors (mirror the dbus stub's key scheme)
_key() { printf '%s' "$1" | tr '/' '_'; }
get_dbus() { cat "$DBUS_STATE/$(_key "$1")" 2>/dev/null; }
set_dbus() { printf '%s' "$2" > "$DBUS_STATE/$(_key "$1")"; }

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
assert_absent() {
  desc=$1; f=$2
  if [ ! -f "$f" ]; then
    pass=$((pass + 1)); echo "ok   - $desc"
  else
    fail=$((fail + 1)); echo "FAIL - $desc (file exists: $f, content '$(cat "$f")')"
  fi
}

# fresh state for each scenario
reset() {
  rm -rf "$DBUS_STATE" "$TMP/state"
  mkdir -p "$DBUS_STATE" "$TMP/state"
  set_dbus "$MODE_PATH" 1
  set_dbus "$MINSOC_PATH" 25.0
}
SAVED_MODE="$TMP/state/qw_dess_saved_mode"
SAVED_MINSOC="$TMP/state/qw_dess_saved_minsoc"

run() { env QW_STATE_DIR="$TMP/state" "$@" sh "$TARGET"; }

echo "=== scenario 1: off lowers floor 25->20, on restores 25 ==="
reset
env QW_STATE_DIR="$TMP/state" QW_MFRR_MIN_SOC=20 sh "$TARGET" off >/dev/null 2>&1
assert_eq "DESS Mode set OFF"            "0"  "$(get_dbus "$MODE_PATH")"
assert_eq "saved mode is 1"              "1"  "$(cat "$SAVED_MODE" 2>/dev/null)"
assert_eq "floor lowered to 20"          "20" "$(get_dbus "$MINSOC_PATH")"
assert_eq "saved floor is 25"            "25" "$(cat "$SAVED_MINSOC" 2>/dev/null)"
env QW_STATE_DIR="$TMP/state" QW_MFRR_MIN_SOC=20 sh "$TARGET" on >/dev/null 2>&1
assert_eq "DESS Mode restored to 1"      "1"  "$(get_dbus "$MODE_PATH")"
assert_eq "floor restored to 25"         "25" "$(get_dbus "$MINSOC_PATH")"
assert_absent "saved-floor file removed after on" "$SAVED_MINSOC"

echo "=== scenario 2: QW_MFRR_MIN_SOC unset -> floor untouched ==="
reset
env QW_STATE_DIR="$TMP/state" sh "$TARGET" off >/dev/null 2>&1
assert_eq "DESS Mode still set OFF"      "0"    "$(get_dbus "$MODE_PATH")"
assert_eq "floor unchanged at 25"        "25.0" "$(get_dbus "$MINSOC_PATH")"
assert_absent "no saved-floor file"      "$SAVED_MINSOC"

echo "=== scenario 3: live floor already <= Y -> not lowered, not saved ==="
reset
set_dbus "$MINSOC_PATH" 18.0
env QW_STATE_DIR="$TMP/state" QW_MFRR_MIN_SOC=20 sh "$TARGET" off >/dev/null 2>&1
assert_eq "floor left at 18"             "18.0" "$(get_dbus "$MINSOC_PATH")"
assert_absent "no saved-floor file"      "$SAVED_MINSOC"

echo "=== scenario 4: double off keeps original resting floor X=25 ==="
reset
env QW_STATE_DIR="$TMP/state" QW_MFRR_MIN_SOC=20 sh "$TARGET" off >/dev/null 2>&1
env QW_STATE_DIR="$TMP/state" QW_MFRR_MIN_SOC=20 sh "$TARGET" off >/dev/null 2>&1
assert_eq "floor still 20 after 2nd off" "20" "$(get_dbus "$MINSOC_PATH")"
assert_eq "saved floor still 25"         "25" "$(cat "$SAVED_MINSOC" 2>/dev/null)"
env QW_STATE_DIR="$TMP/state" QW_MFRR_MIN_SOC=20 sh "$TARGET" on >/dev/null 2>&1
assert_eq "floor restored to 25 (not 20)" "25" "$(get_dbus "$MINSOC_PATH")"

echo "=== scenario 5: on with no prior off is a safe no-op for floor ==="
reset
env QW_STATE_DIR="$TMP/state" QW_MFRR_MIN_SOC=20 sh "$TARGET" on >/dev/null 2>&1
assert_eq "floor unchanged at 25"        "25.0" "$(get_dbus "$MINSOC_PATH")"
assert_eq "DESS Mode default-restored 1" "1"    "$(get_dbus "$MODE_PATH")"

echo "-----------------------------------------"
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
