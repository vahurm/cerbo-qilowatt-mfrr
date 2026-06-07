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
   before turning it off and restores exactly that value.
3. **Watchdog** — `qw_dess_watchdog.sh`, run every minute, forces DESS back on if it
   has been off longer than `QW_MAX_OFF_SECS` (default 1800 s). This protects against
   a crashed agent leaving DESS off forever.
4. **State-machine failsafes** — the agent's Python state machine
   (`mfrr_statemachine.py`) releases the setpoint and restores DESS if the Qilowatt
   connection is lost for `QW_MQTT_LOST_FAILSAFE_S` (default 5 min) or an event runs
   longer than `QW_MAX_EVENT_S` (default 30 min). On a clean stop the agent also
   reverts any active event. (The optional Node-RED flow carries an equivalent
   failsafe.)

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
