#!/bin/sh
# =============================================================================
# afrr_capture.sh — durable, read-only WORKMODE tap for aFRR verification
# =============================================================================
# Appends only "WORKMODE received" lines from the qw-agent log to a file on
# /data, so the full command history survives multilog rotation (the ring only
# keeps ~10 files) and reboots. It is STRICTLY read-only w.r.t. the agent: it
# never writes dbus and never runs the actuators.
#
# Busybox notes (Venus OS): `grep --line-buffered` is unsupported, so we filter
# with awk + per-line fflush() for durable, unbuffered appends.
#
#   afrr_capture.sh start    # seed history once, then follow in the background
#   afrr_capture.sh status   # show pid + captured line count
#   afrr_capture.sh tap      # foreground follower (used internally by start)
#
# Classify whatever has been captured so far:
#   python3 /data/qw-agent/afrr_probe.py --log /data/afrr-workmode.log
# =============================================================================
OUT=/data/afrr-workmode.log
LOG=/var/log/qw-agent/current
ARCHIVE_GLOB=/var/log/qw-agent/@*.s

case "${1:-start}" in
  tap)
    # Seed the full history once (archived rotations + current). tail -n0 below
    # then follows only NEW lines, so there are no duplicates.
    if [ ! -s "$OUT" ]; then
      cat $ARCHIVE_GLOB "$LOG" 2>/dev/null | grep "WORKMODE received" > "$OUT" 2>/dev/null
    fi
    tail -n0 -F "$LOG" 2>/dev/null | awk '/WORKMODE received/{print; fflush()}' >> "$OUT"
    ;;
  start)
    if pgrep -f "afrr_capture.sh tap" >/dev/null 2>&1; then
      echo "afrr capture already running (pid $(pgrep -f 'afrr_capture.sh tap' | tr '\n' ' '))"
      exit 0
    fi
    nohup /data/afrr_capture.sh tap >/dev/null 2>&1 &
    sleep 1
    echo "afrr capture started (pid $(pgrep -f 'afrr_capture.sh tap' | tr '\n' ' ')); output $OUT"
    ;;
  status)
    if pgrep -f "afrr_capture.sh tap" >/dev/null 2>&1; then
      echo "running (pid $(pgrep -f 'afrr_capture.sh tap' | tr '\n' ' '))"
    else
      echo "not running"
    fi
    [ -f "$OUT" ] && echo "$(wc -l < "$OUT") WORKMODE lines in $OUT"
    ;;
  *)
    echo "usage: $0 {start|status|tap}" >&2
    exit 2
    ;;
esac
