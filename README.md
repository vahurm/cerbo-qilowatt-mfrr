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

Telemetry needs no per-site profile: the default `auto` sums every PV source
Venus knows about (DC MPPTs, PV inverters on the AC output and on the grid
side), so DC-coupled, AC-coupled and mixed sites all work out of the box.

> **Safety first.** This software commands real grid power flows (battery
> charge/discharge, up to several kW import/export) and toggles Dynamic ESS.
> Read [`docs/SAFETY.md`](docs/SAFETY.md) before deploying.

## Up and running in 10 minutes

From a workstation with `ssh` and `python3 -m pip` (the Cerbo itself needs no
internet and no pip):

```sh
git clone https://github.com/vahurm/cerbo-qilowatt-mfrr && cd cerbo-qilowatt-mfrr
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh          # asks for id/user/pass/caps
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --dry-run-window 120   # nothing actuated
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --restart               # go live
```

`install.sh` copies the scripts, agent, vendored libs and service to `/data`
(survives firmware updates), adds the boot hooks, writes a `VERSION` stamp and
ends with `qw_doctor.sh` — a PASS/WARN/FAIL self-check of the site. Fix every
FAIL before `--restart`. Then watch:

```sh
ssh root@<cerbo-ip> tail -F /var/log/qw-agent/current
ssh root@<cerbo-ip> /data/qw_doctor.sh          # any time
```

Before going live you can also replay your own captured command stream to see
what the agent *would* have done: `python3 tools/replay.py --log afrr-workmode.log`.

### What your site must have

| Requirement | Why | Check |
|-------------|-----|-------|
| Cerbo GX / Ekrano GX (Venus OS ≥ v3.x, `python3` + `python3-dbus`) | the agent runs on it | `qw_doctor.sh` → `python3-dbus importable` |
| Victron ESS (VE.Bus MultiPlus/Quattro, ESS assistant) | `AcPowerSetPoint` is how power is commanded | `/Settings/CGwacs/AcPowerSetPoint` readable |
| Dynamic ESS available (installed or at least the setting present) | it is switched off during events and restored after | `/Settings/DynamicEss/Mode` readable |
| ESS mode **not** "Keep batteries charged" | otherwise the setpoint cannot discharge | `BatteryLife/State != 9` |
| Battery monitor with SOC on `com.victronenergy.system` | no SOC → no telemetry is published | `/Dc/Battery/Soc` readable |
| Grid meter (or Multi-measured grid) | ENERGY block, Today/Total | `/Ac/Grid/L1/Power` readable |
| Qilowatt device: **inverter id (UUID)**, MQTT user/password | the cloud link | from Qilowatt support / portal |
| Your grid connection's import and export limits in W | `QW_MAX_IMPORT_W` / `QW_MAX_EXPORT_W` | fuse rating × 230 V × phases; doctor suggests |

### Tested hardware

| Setup | Status |
|-------|--------|
| 3× MultiPlus-II 48/5000 (3-phase) + Cerbo GX, Venus OS v3.75, DESS, DC-coupled PV (Victron MPPT) + small AC PV inverter | in production since 2026-07 |
| 3× MultiPlus-II 48/5000 (3-phase) + Cerbo GX, Venus OS v3.75, DESS, AC-coupled PV (Huawei, curtailed via Node-RED) | in production since 2026-07 |
| Multi RS / Ekrano GX / single-phase Multi | same dbus paths, **untested** — run `qw_doctor.sh` and a dry-run window, report back |

## How it works

```
mqtt.qilowatt.it:8883 (TLS)
        │  WORKMODE in  /  SENSOR,STATE,STATUS0 out
        ▼
┌─────────────────────────── Cerbo GX ───────────────────────────┐
│  qw_agent.py (qilowatt-py)                                      │
│    • receives WORKMODE backlog commands                         │
│    • reports telemetry from dbus (auto PV profile; none w/o SOC) │
│    • mfrr_statemachine.py  (IDLE / ACTIVE[frr|trade] + failsafes)│
│    • → /data/qw_*.sh actuators, read back, retry, degraded flag │
│    • start-up recovery + config sanity check, state.json        │
│    • (optional) republishes WORKMODE → local MQTT for Node-RED  │
│                          │                                      │
│                          ▼                                      │
│  /data/qw_dess_toggle.sh   (DESS Mode off/on)                  │
│  /data/qw_grid_setpoint.sh (AcPowerSetPoint, asym ±limit)     │
│  /data/qw_dess_watchdog.sh (failsafe: force DESS back on)      │
│  /data/qw_log_audit.sh     (hourly log review, optional push)  │
│  /data/qw_doctor.sh        (PASS/WARN/FAIL self-check)         │
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
| (event state)         | `qw/mfrr_active`, `qw/mfrr_kind`, `qw/mfrr_signed_w`, `qw/mfrr_degraded` | `on`/`off`, `frr`/`trade`/`none`, signed W, `true`/`false` |
| (liveness)            | `qw/online`         | retained `true`/`false` (LWT)    |

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
agent/        qw_agent.py, mfrr_statemachine.py, actuators.py (read-back), startup.py
              (recovery + config sanity), telemetry/ (auto + legacy profiles)
scripts/      qw_dess_toggle.sh, qw_grid_setpoint.sh, qw_dess_watchdog.sh,
              qw_log_audit.sh (hourly audit, QW_ALERT_URL), qw_doctor.sh (self-check)
tools/        afrr_probe.py (WORKMODE classifier), replay.py (offline "what would
              the agent have done"), afrr_capture.sh (durable WORKMODE tap)
nodered/      curtailment-mfrr-aware.md — how a Node-RED flow consumes the bridge
contrib/      nodered-legacy/ — the original Node-RED orchestrator (do not run with the agent)
service/      daemontools service template for the daemon
deploy/       install.sh (--restart, --dry-run-window N, --uninstall, --pylib-tarball)
docs/         ARCHITECTURE.md, INSTALL.md, SAFETY.md, TROUBLESHOOTING.md, AFRR_VERIFICATION.md
tests/        pytest suite + POSIX-sh tests for every script
.env.example  per-site config template (real values stay untracked)
Makefile      make test / lint / release / replay
```

## Manual path (if you prefer not to use install.sh)

1. Read [`docs/SAFETY.md`](docs/SAFETY.md) and [`docs/INSTALL.md`](docs/INSTALL.md).
2. Copy `.env.example` to a private `/data/qw-agent.env` on the Cerbo and fill in
   your Qilowatt inverter id (UUID) + MQTT credentials.
3. Set the asymmetric `QW_MAX_IMPORT_W` / `QW_MAX_EXPORT_W` limits for your
   site (the agent warns at start-up while they are unset or at the example
   value). Decide whether the site should honour Q trades (`QW_TRADE_MODES`,
   default `buy,sell`; see SAFETY.md).
4. Deploy the scripts and the daemon (see `docs/INSTALL.md`); validate with
   `QW_DRY_RUN=1` before going live; run `/data/qw_doctor.sh`. Node-RED is optional.

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)).
No credentials live in this repository — per-site values stay in an untracked
`.env`.

## Status

In production on two Victron ESS sites since 2026-07 (3× MultiPlus-II + DESS
each; one DC-coupled, one AC-coupled PV), dispatched through KratTrade (`kratt`)
and Qilowatt's own desk (`qilowatt`), with Q trades actuated since 2026-09.
Every measured number in the docs and comments comes from those two sites'
logs; your site will differ — measure with `tools/afrr_probe.py` before
copying the values.

Telemetry field mapping should be validated against the Qilowatt `SENSOR`
payload for your specific system before relying on market settlement. Not
affiliated with Qilowatt; use at your own risk (see `docs/SAFETY.md`).

Something not working? [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)
maps symptoms to causes; when opening an issue, paste `qw_doctor.sh` output.
Changes per version: [`CHANGELOG.md`](CHANGELOG.md).

## Credits & license

Built on the MIT-licensed [`qilowatt-py`](https://github.com/qilowatt/qilowatt-py)
by Qilowatt. This project is licensed under the [MIT License](LICENSE).
