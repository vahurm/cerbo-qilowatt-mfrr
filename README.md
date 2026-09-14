# cerbo-qilowatt-mfrr

Run [Qilowatt](https://qilowatt.eu) mFRR (manual Frequency Restoration Reserve)
participation **entirely on a Victron Cerbo GX**, with **no Home Assistant** in the
loop.

A small Python daemon owns the Qilowatt cloud link (using the official
[`qilowatt-py`](https://pypi.org/project/qilowatt/) library), reports telemetry
from the Victron dbus, runs the mFRR state machine **in Python**, and drives the
Victron actuators (DESS toggle + grid setpoint). **No Node-RED and no Venus OS
Large are required** — a standard Venus OS image is enough. Node-RED is optional,
for a co-resident curtailment flow or dashboard.

PV topology differs per site, so telemetry is read through a **profile**
(`QW_TELEMETRY_PROFILE`): `dc_coupled` (PV on the battery DC bus via Victron
MPPT, e.g. site A) or `ac_coupled` (PV inverter on the AC output, e.g. site B).

> **Safety first.** This software commands real grid power flows (battery
> charge/discharge, up to several kW import/export) and toggles Dynamic ESS.
> Read [`docs/SAFETY.md`](docs/SAFETY.md) before deploying.

## How it works

```
mqtt.qilowatt.it:8883 (TLS)
        │  WORKMODE in  /  SENSOR,STATE,STATUS0 out
        ▼
┌─────────────────────────── Cerbo GX ───────────────────────────┐
│  qw_agent.py (qilowatt-py)                                      │
│    • receives WORKMODE backlog commands                         │
│    • reports telemetry from dbus via a profile (dc/ac_coupled)  │
│    • mfrr_statemachine.py  (IDLE / ACTIVE[frr|trade] + failsafes)│
│    • → /data/qw_*.sh actuators                                  │
│    • (optional) republishes WORKMODE → local MQTT for Node-RED  │
│                          │                                      │
│                          ▼                                      │
│  /data/qw_dess_toggle.sh   (DESS Mode off/on)                  │
│  /data/qw_grid_setpoint.sh (AcPowerSetPoint, asym ±limit)     │
│  /data/qw_dess_watchdog.sh (failsafe: force DESS back on)      │
└────────────────────────────────────────────────────────────────┘
```

The `WORKMODE` command drives the state machine directly. With the optional
local bridge (`QW_LOCAL_BRIDGE=1`) it is also republished for Node-RED / dashboards:

| WorkModeCommand field | Local MQTT topic    | Meaning                          |
|-----------------------|---------------------|----------------------------------|
| `_source`             | `qw/qw_source`      | `fusebox`/`kratt`/`qilowatt`/…   |
| `Mode`                | `qw/qw_mode`        | `frrup`/`frrdown`/`buy`/`sell`/`normal` |
| `PowerLimit`          | `qw/qw_powerlimit`  | watts                            |
| (connection state)    | `qw/qw_connected`   | `on`/`off`                       |
| (event state)         | `qw/mfrr_active`, `qw/mfrr_kind`, `qw/mfrr_signed_w` | `on`/`off`, `frr`/`trade`/`none`, signed W |

## What the agent actuates

Two kinds of event share one `ACTIVE` state and the same DESS-off → setpoint →
release path:

| Kind    | `_source` (gate)            | `Mode`             | Setpoint             | SOC floor | Ends on |
|---------|-----------------------------|--------------------|----------------------|-----------|---------|
| `frr`   | `QW_MFRR_SOURCES`           | `frrup` / `frrdown`| −P export / +P import| lowered to `QW_MFRR_MIN_SOC` | 0 W stand-down, non-event command, `QW_MAX_EVENT_S`, link lost |
| `trade` | `QW_TRADE_SOURCES` (`qilowatt`) | `buy` / `sell` (`QW_TRADE_MODES`) | +P import / −P export | untouched | same, plus live SOC reaching the command's `BatterySoc`, `QW_MAX_TRADE_S` |

`trade` is Qilowatt's SOC preparation between balancing activations — the portal
labels it **Q** / BUY; on the wire it is `_source: qilowatt`, `Mode: buy`,
`BatterySoc: <target>`. On site A it arrives 5–30 min after a `kratt/frrup`
stand-down and the next `frrup` follows 5–30 min later. A command of the other
kind switches the event in place (setpoint rewritten, no DESS on/off in between).
Every other Mode from any source — `savebattery`, `limitexport`, `normal`, … —
is dropped and ends a running event. Set `QW_TRADE_MODES=` (empty) to drop
trades as well.

`PowerLimit` is capped to `QW_MAX_IMPORT_W` / `QW_MAX_EXPORT_W` in the agent
before it reaches `qw_grid_setpoint.sh`, whose clamp would otherwise *reject* an
oversized request and leave the previous setpoint in place.

## Repository layout

```
agent/        qw_agent.py, mfrr_statemachine.py, actuators.py,
              telemetry/ (base + dc_coupled + ac_coupled profiles), requirements.txt
scripts/      qw_dess_toggle.sh, qw_grid_setpoint.sh, qw_dess_watchdog.sh, qw_log_audit.sh
tools/        afrr_probe.py (read-only WORKMODE classifier: FRR / Q trade / unknown)
nodered/      flow.json (optional curtailment/dashboard) + curtailment-mfrr-aware.md
service/      daemontools service template for the daemon
deploy/       install.sh (rsync + restart over SSH)
docs/         ARCHITECTURE.md, INSTALL.md, SAFETY.md, AFRR_VERIFICATION.md
tests/        pytest suite + POSIX-sh tests for the three scripts
.env.example  per-site config template (real values stay untracked)
```

## Quick start

1. Read [`docs/SAFETY.md`](docs/SAFETY.md) and [`docs/INSTALL.md`](docs/INSTALL.md).
2. Copy `.env.example` to a private `/data/qw-agent.env` on the Cerbo and fill in
   your Qilowatt `device_id` + MQTT credentials.
3. Set `QW_TELEMETRY_PROFILE` and the asymmetric `QW_MAX_IMPORT_W` /
   `QW_MAX_EXPORT_W` limits for your site. Decide whether the site should
   honour Q trades (`QW_TRADE_MODES`, default `buy,sell`; see SAFETY.md).
4. Deploy the actuator scripts and the daemon (see `docs/INSTALL.md`); validate
   with `QW_DRY_RUN=1` before going live. Node-RED is optional.

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)).
No credentials live in this repository — per-site values stay in an untracked
`.env`.

## Status

Pre-release. Piloted on a Victron Cerbo GX (3× MultiPlus-II + DESS). Telemetry
field mapping should be validated against the Qilowatt `SENSOR` payload for your
specific system before relying on market settlement.

## Credits & license

Built on the MIT-licensed [`qilowatt-py`](https://github.com/qilowatt/qilowatt-py)
by Qilowatt. This project is licensed under the [MIT License](LICENSE).
