#!/bin/sh
# =============================================================================
# test_grid_setpoint.sh — POSIX-sh tests for scripts/qw_grid_setpoint.sh
# =============================================================================
# No bats dependency. `dbus` and `logger` are stubbed via PATH so the
# safety-critical asymmetric clamp can be exercised off a Cerbo.
#
#   sh tests/test_grid_setpoint.sh   # exit 0 = all passed
# =============================================================================

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
TARGET="$REPO/scripts/qw_grid_setpoint.sh"

if [ ! -f "$TARGET" ]; then
  echo "FATAL: target not found: $TARGET" >&2
  exit 99
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

# --- PATH stubs ------------------------------------------------------------ #
cat > "$TMP/dbus" <<'EOF'
#!/bin/sh
echo "$*" >> "$DBUS_LOG"
case "$*" in
  *GetValue*) echo "value = 0" ;;
esac
exit 0
EOF
cat > "$TMP/logger" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$TMP/dbus" "$TMP/logger"

DBUS_LOG="$TMP/dbus.log"
export DBUS_LOG
PATH="$TMP:$PATH"
export PATH

pass=0
fail=0

# check <description> <expected_exit> <command...>
check() {
  desc=$1
  want=$2
  shift 2
  : > "$DBUS_LOG"
  "$@" >/dev/null 2>&1
  got=$?
  if [ "$got" -eq "$want" ]; then
    pass=$((pass + 1))
    echo "ok   - $desc (exit $got)"
  else
    fail=$((fail + 1))
    echo "FAIL - $desc (want $want, got $got)"
  fi
}

# --- argument validation --------------------------------------------------- #
check "no argument prints usage"          1 sh "$TARGET"
check "non-integer is rejected"           2 sh "$TARGET" abc
check "float string is rejected"          2 sh "$TARGET" 1000.5

# --- default symmetric limits (15000 / 15000) ------------------------------ #
check "import over default limit"         3 sh "$TARGET" 16000
check "export over default limit"         3 sh "$TARGET" -16000
check "import at default limit ok"        0 sh "$TARGET" 15000
check "export at default limit ok"        0 sh "$TARGET" -15000

# --- Asymmetric limits (import 28000 / export 15000) ------------------------ #
check "asym import 28000 accepted"        0 \
  env QW_MAX_IMPORT_W=28000 QW_MAX_EXPORT_W=15000 sh "$TARGET" 28000
check "asym import 28001 rejected"        3 \
  env QW_MAX_IMPORT_W=28000 QW_MAX_EXPORT_W=15000 sh "$TARGET" 28001
check "asym export 15000 accepted"        0 \
  env QW_MAX_IMPORT_W=28000 QW_MAX_EXPORT_W=15000 sh "$TARGET" -15000
check "asym export 15001 rejected"        3 \
  env QW_MAX_IMPORT_W=28000 QW_MAX_EXPORT_W=15000 sh "$TARGET" -15001

# --- normal operations ----------------------------------------------------- #
check "zero release accepted"             0 sh "$TARGET" 0
check "get reads current value"           0 sh "$TARGET" get

# --- a valid write actually calls dbus SetValue with the value ------------- #
: > "$DBUS_LOG"
sh "$TARGET" 3000 >/dev/null 2>&1
if grep -q "SetValue %3000" "$DBUS_LOG"; then
  pass=$((pass + 1))
  echo "ok   - in-limit 3000 calls dbus SetValue %3000"
else
  fail=$((fail + 1))
  echo "FAIL - in-limit 3000 did not call dbus SetValue %3000"
  echo "       dbus log was:"; sed 's/^/         /' "$DBUS_LOG"
fi

# --- a rejected write must NOT touch dbus SetValue ------------------------- #
: > "$DBUS_LOG"
sh "$TARGET" 99999 >/dev/null 2>&1
if grep -q "SetValue" "$DBUS_LOG"; then
  fail=$((fail + 1))
  echo "FAIL - rejected 99999 wrote a setpoint (should not)"
else
  pass=$((pass + 1))
  echo "ok   - rejected 99999 does not write a setpoint"
fi

echo "-----------------------------------------"
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
