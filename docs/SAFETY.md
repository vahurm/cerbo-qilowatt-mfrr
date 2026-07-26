# Safety

This software commands **real grid power flows** and changes inverter behaviour.
Read this before deploying.

## What it controls

- **Dynamic ESS Mode** (`/Settings/DynamicEss/Mode`) — turned off for the duration
  of an mFRR event and restored afterwards.
- **AC power setpoint** (`/Settings/CGwacs/AcPowerSetPoint`) — drives the
  MultiPlus-II to import or export power, up to several kW, across all phases.

A wrong setpoint, a stuck "off" state, or an uncontrolled export can trip
protection, exceed your grid connection capacity, or cause unwanted import/export
charges.

## Built-in safety layers

1. **Asymmetric setpoint clamp** — `qw_grid_setpoint.sh` rejects any import
   (positive) `> QW_MAX_IMPORT_W` and any export (`|negative|`) `> QW_MAX_EXPORT_W`
   (both default 15000). Set them to your site's grid-connection import capacity
   and feed-in (export) cap respectively.
2. **Atomic DESS save/restore** — `qw_dess_toggle.sh` saves the original DESS Mode
   before turning it off and restores exactly that value. The same save/restore
   covers the ESS minimum-SOC floor when `QW_MFRR_MIN_SOC` is set, with two
   guards: the floor is only ever lowered, never raised, and it is only restored
   if it still holds the value the script installed — so a floor change made in
   VRM during an event is kept rather than silently reverted.
3. **Watchdog** — `qw_dess_watchdog.sh`, run every minute, forces DESS back on if it
   has been off longer than `QW_MAX_OFF_SECS` (default 7800 s). This protects against
   a crashed agent leaving DESS off forever.
4. **State-machine failsafes** — the agent's Python state machine
   (`mfrr_statemachine.py`) releases the setpoint and restores DESS if the Qilowatt
   connection is lost for `QW_MQTT_LOST_FAILSAFE_S` (default 5 min) or an event runs
   longer than `QW_MAX_EVENT_S` (default 2 h). On a clean stop the agent also
   reverts any active event. (The optional Node-RED flow carries an equivalent
   failsafe.)

   These two limits are a pair and the ordering matters: the agent's cap must stay
   **below** the watchdog's, so the agent is always the one that ends an event —
   releasing the grid setpoint first, then restoring DESS and the SOC floor. If the
   watchdog fires first, DESS and the arbitrage floor come back while the agent
   still holds the setpoint and believes the event is running.

   Size `QW_MAX_EVENT_S` from your own logs rather than intuition. Both defaults
   were originally 1800 s, which real dispatch outgrew: across 179 Kungla events
   (2026-07-04 .. 07-26) the median was 620 s but 11 ran longer than 1800 s, the
   longest 6292 s, and the failsafe truncated 10 live events mid-delivery. To
   audit your own site:

   ```sh
   grep -c FAILSAFE /var/log/qw-agent/current /var/log/qw-agent/@*.s
   ```

5. **Connection watchdogs** — `ConnectionWatchdog` exits (so the supervisor restarts
   the agent with a fresh session) on three separate signs of deafness: the link
   reported down past `QW_LINK_RESTART_S`, the transport connected while the command
   topic stays unsubscribed past `QW_SUBSCRIBE_GRACE_S`, and no command at all for
   `QW_IDLE_REFRESH_S` while IDLE. The first two are cheap and reliable; the third
   is a last resort and the only one that can misfire.

   Size `QW_IDLE_REFRESH_S` above the site's longest genuine command silence, and
   note that setting it too low destroys the evidence needed to correct it: the
   portal pushes a snapshot ~20 s after every reconnect, so each refresh
   manufactures a command that resets the timer and the loop sustains itself.
   Kirdalu restarted every 6 h for weeks this way — 49 of 70 restarts — while its
   measured median gap simply mirrored the setting. Measure it instead of guessing:

   ```sh
   python3 /data/qw-agent/afrr_probe.py --log /data/afrr-workmode.log
   ```

## Operator responsibilities

- **Set `QW_MAX_IMPORT_W` / `QW_MAX_EXPORT_W` correctly** for the physical
  connection (import capacity and feed-in cap). Do not rely on the defaults.
- **Pick the right `QW_TELEMETRY_PROFILE`** (`dc_coupled` vs `ac_coupled`) so PV
  power is read from the correct dbus path.
- **Validate telemetry** (`SENSOR` payload) against your real system before relying
  on market settlement — incorrect telemetry can misrepresent available flexibility.
- **Dry-run first** (`QW_DRY_RUN=1`) and start with small values before going live.
- **Keep the watchdog running** at all times (boot loop). It is the last line of
  defence.
- **Check that the two-level SOC floor actually engages** if you set
  `QW_MFRR_MIN_SOC`: it must be below the dashboard's Minimum SOC slider, and the
  log says `Lowered SOC floor X% -> Y%` when it works and `nothing to lower` when
  it does not.
- **Never run two orchestrators at once** (e.g. an old HA automation, the agent's
  state machine, and a Node-RED actuator flow) — they write the same dbus paths and
  will race.

## Emergency stop

To immediately return the system to normal:

```sh
/data/qw_dess_toggle.sh on
/data/qw_grid_setpoint.sh 0
```

Then stop the agent service (`svc -d /service/qw-agent`) — its clean shutdown
already reverts any active event — and disable the Node-RED flow tab if present.

## Disclaimer

Provided "as is", without warranty. See [LICENSE](../LICENSE). You are responsible
for safe operation, compliance with your grid code and connection agreement, and any
aggregator/market obligations.
