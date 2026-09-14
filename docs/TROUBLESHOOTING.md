# Troubleshooting — symptom → cause → what to do

Start with `ssh root@<cerbo> /data/qw_doctor.sh`. It prints PASS/WARN/FAIL
lines for the runtime, the dbus paths, ESS/DESS state, the env file, the
service and the boot hooks, and it names the fix next to each finding. Then
read the log: `tail -n 200 /var/log/qw-agent/current`. The hourly
`qw_log_audit.sh` reports the same patterns to syslog (`logread | grep qw_health`)
and, if `QW_ALERT_URL` is set, pushes them to you.

| Symptom | Cause | Do |
|---------|-------|----|
| Portal shows the device **offline** / no data | `telemetry unavailable` in the log: dbus or `/Dc/Battery/Soc` unreadable, so the agent publishes nothing (an all-zero SENSOR would be worse) | `qw_doctor.sh`; check the battery monitor is selected in Settings → System setup; `python3 -c 'import dbus'` |
| Portal shows the device online but **status "Unknown"** | Normal for Victron — there is no single inverter-status register, the reference integration sends a constant | nothing |
| Dashboard empty **and** no commands ever arrive | Wrong `QW_DEVICE_ID`: it must be the **inverter id (UUID)**, not the account device_id hex | fix `/data/qw-agent.env`, restart |
| Log: `Missing required config: QW_...` and the service flaps | `/data/qw-agent.env` still has `REPLACE_WITH_*` or is missing | edit the file, `chmod 600`, restart |
| Log: `STARTUP RECOVERY: previous run left an event open` | A previous process died mid-event (crash, `kill -9`, reboot). The agent restored setpoint 0 + DESS on before connecting | nothing; if it recurs, look for the `Traceback` above it |
| Log: `CONFIG WARN: QW_MAX_IMPORT_W/QW_MAX_EXPORT_W not set` / `at the example value` | Caps not set for this grid connection; the setpoint script's built-in 15000 W limit *rejects* instead of capping | set both to the connection's real limits (doctor prints a suggestion from the AC input current limit) |
| Log: `CONFIG WARN: QW_MAX_EVENT_S ... not below the DESS watchdog cap` | Event cap ≥ `QW_MAX_OFF_SECS`: the watchdog would restore DESS mid-event while the agent still holds the setpoint | lower `QW_MAX_EVENT_S`/`QW_MAX_TRADE_S` or raise `QW_MAX_OFF_SECS` in the env |
| Log: `REJECT: N W (import/export over M)` | The market asked more than `qw_grid_setpoint.sh`'s limit and the agent-side cap was not set — **nothing was delivered** | set `QW_MAX_IMPORT_W`/`QW_MAX_EXPORT_W` so the agent caps first |
| Log: `actuation failed: setpoint ... -> DEGRADED` | The write did not read back (dbus hiccup, another controller overwriting `AcPowerSetPoint`, ESS in a mode that ignores it) | `qw_grid_setpoint.sh get`; check `Hub4Mode` (external control) and for a second orchestrator (Node-RED legacy flow, HA) |
| Log: `actuation failed: DESS on` after an event | DESS Mode did not go back to the saved value | `qw_dess_toggle.sh status`; `qw_dess_toggle.sh on` by hand; watchdog restores within `QW_MAX_OFF_SECS` anyway |
| Log: `nothing to lower` | `QW_MFRR_MIN_SOC` (Y) is not below the dashboard Minimum SOC (X) | set Y < X, or leave `QW_MFRR_MIN_SOC` empty for a single floor |
| Log: `dropping FRR dispatch from unlisted source 'xyz'` | A dispatcher not in `QW_MFRR_SOURCES` is sending frrup/frrdown — every activation is being lost | add it to `QW_MFRR_SOURCES` (only if it is your balancing dispatcher; never `optimizer`) |
| Log: `ignoring non-FRR Mode 'buy' from mFRR source 'qilowatt'` | Q trades are disabled (`QW_TRADE_MODES` empty) — the battery stays at the mFRR floor between activations | set `QW_TRADE_MODES=buy,sell` unless you have a reason not to (SAFETY.md) |
| Log: `FAILSAFE: frr event > 7200s` | Either a real >2 h activation truncated, or the return-to-normal never arrived | check `afrr_probe.py` for the site's real event lengths; raise `QW_MAX_EVENT_S` (keep it below the watchdog cap) |
| Log: `FAILSAFE: QW link lost > 300s while ACTIVE` | Internet/broker outage mid-event | nothing to fix locally; the event was reverted to normal on purpose |
| Log: `no WORKMODE command received for N s ... refreshing` every X hours | `QW_IDLE_REFRESH_S` is below the site's real command silence — the restart manufactures its own snapshot command | measure with `afrr_probe.py --log /data/afrr-workmode.log`; raise or set 0 |
| Log: `QW transport connected but command subscription dead` | qilowatt-py's zombie subscription; the agent restarted itself | nothing; if hourly, report with the log |
| Log: `QW connect attempt 1/5 failed: ... name resolution` at boot | Network not up yet when the service started | nothing; it retries with backoff |
| Agent restarts every few seconds | `Traceback` in the log | read it; usually a missing vendored lib (`pylib`) → re-run `install.sh` |
| Device offline after a **Venus OS firmware update** | `/service` and `/var/log` are wiped; `/data/rc.local` must re-link the service | `qw_doctor.sh` (checks the hooks); re-run `install.sh` |
| DESS stays off for hours after an event | Watchdog loop not running (rc.local hook present but never started) or agent dead | `qw_doctor.sh` → `qw_dess_watchdog loop`; `qw_dess_toggle.sh on` |
| Curtailment flow (Node-RED) never stands down | `QW_LOCAL_BRIDGE=0`, or the flow subscribes to a different prefix | set `QW_LOCAL_BRIDGE=1`, `mosquitto_sub -t 'qw/#' -v` |
| Curtailment stands down during a Q `buy` | By design: `mfrr_active` is `on` for trades too (PV at 100 % during a buy costs nothing) | read `qw/mfrr_kind` in the flow if you want to differ |
| `qw_health: WARN — Q trades started but NO mFRR dispatch` | The site is being pre-charged but not (or no longer) dispatched — an economic, not a technical, problem | check the portal / your aggregator |
| `qw_health: WARN — an event was ended by a foreign automation` | Something else writes the WorkMode channel (e.g. Qilowatt's Energy Optimizer on 15-min slots) | see SAFETY.md; ask Qilowatt to exclude the site from the optimizer during FRR |
| Dry-run window shows `[dry-run] DESS off` but nothing on dbus | That is the point of dry-run (`QW_DRY_RUN=1`) | `install.sh --restart` when satisfied |

## Reading the state without MQTT

`/data/qw-agent/state.json`:

```json
{"state": "ACTIVE", "kind": "frr", "signed_w": -12000, "degraded": false,
 "updated_at": 1789000000, "version": "1.0.0"}
```

It is rewritten on every state change and at start-up; `qw_doctor.sh` prints
and cross-checks it.

## What to include in an issue

1. `/data/qw_doctor.sh` output.
2. `tail -n 300 /var/log/qw-agent/current` (credentials are never logged).
3. `/data/qw-agent/VERSION`.
4. If about a command the agent did or did not act on: the `WORKMODE received`
   line and, ideally, `python3 tools/replay.py --log <that log>`.
