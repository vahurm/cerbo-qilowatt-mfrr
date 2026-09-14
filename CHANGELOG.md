# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are git tags
and the agent reports its `__version__` (plus the install's git SHA from
`/data/qw-agent/VERSION`) at start-up.

## [1.0.0] — 2026-09-14

First tagged release: the state the two production sites run, hardened for
installation on a site the authors have never seen.

### Added
- **Start-up recovery** (`agent/startup.py`): a process that died mid-event
  (crash, `kill -9`, reboot) is detected from the toggle script's state files;
  the agent writes setpoint 0 + DESS on before connecting (`STARTUP RECOVERY`).
- **Actuator read-back**: `ScriptActuator` reads AcPowerSetPoint / DESS Mode
  back after every write and returns a bool; the state machine retries once,
  then logs `actuation failed`, raises a `degraded` flag (bridge topic
  `qw/mfrr_degraded`, `state.json`) and keeps tracking the event.
- **Telemetry guard**: no SENSOR is published while dbus or `/Dc/Battery/Soc`
  is unreadable (`telemetry unavailable`, once a minute) instead of zeros.
- **Config sanity at start-up** (`CONFIG WARN`): event caps vs watchdog cap,
  missing/example import-export caps, `QW_MFRR_MIN_SOC` vs live floor, trades
  disabled.
- `scripts/qw_doctor.sh`: PASS/WARN/FAIL self-check (runtime, dbus paths,
  DESS/ESS state, env file, caps consistency with `MaxFeedInPower` and the AC
  input current limit, leftover state, service link, boot hooks, loops, clock,
  recent log). Installed to `/data`, run at the end of `install.sh`.
- `deploy/install.sh`: `--restart`, `--dry-run-window N`, `--uninstall
  [--purge]`, `--pylib-tarball` (no pip on the workstation, see `make release`),
  interactive env creation, `/data/qw-agent/VERSION` stamp.
- Telemetry profile `auto` (default): DC MPPT + `PvOnOutput` + `PvOnGrid`
  summed on `com.victronenergy.system`; `dc_coupled`/`ac_coupled` remain as
  aliases. `ENERGY.Today/Total` from the grid meter's lifetime import counter
  (midnight baseline persisted).
- `QW_STATE_FILE` (`/data/qw-agent/state.json`): state, kind, signed W,
  degraded, version — for the doctor, dashboards and Node-RED without MQTT.
- `QW_ALERT_URL`: `qw_log_audit.sh` POSTs WARN/ERROR findings (ntfy, webhooks).
- Audit patterns: `actuation failed`, `telemetry unavailable`,
  `STARTUP RECOVERY`, `CONFIG WARN`, `dropping FRR dispatch`, and "N trades
  with no mFRR dispatch in the window".
- `tools/replay.py`: run a captured WORKMODE log through the state machine
  offline — events the agent would have run, DESS-off hours, cap/failsafe
  hits, dropped sources.
- Tests: bridge contract (`qw/mfrr_active`, `qw/online`, … names and
  payloads pinned), startup, telemetry loop, energy totals, doctor (sh),
  replay; CI matrix Python 3.8/3.10/3.12 + shellcheck.
- Docs: README quick start + site requirements + tested hardware,
  `docs/TROUBLESHOOTING.md`, `CONTRIBUTING.md`, issue template, `Makefile`.

### Changed
- `QW_MFRR_SOURCES` default is now `fusebox,kratt,qilowatt` (a site dispatched
  directly by Qilowatt silently dropped everything before). Dispatch from a
  source not in the list is logged as a WARNING.
- The legacy Node-RED orchestrator moved to `contrib/nodered-legacy/` with a
  "do not run alongside the agent" README; `nodered/` keeps only the
  read-only curtailment integration notes.

## [0.9] — 2026-09 (untagged)
- Q trades (`_source: qilowatt`, `Mode: buy`/`sell`) actuated as a second
  event kind with SOC-target end, no floor change, `QW_MAX_TRADE_S`;
  agent-side import/export caps; `mfrr_kind`/`mfrr_signed_w` bridge topics.
- Zero-power FRR command ends the event instead of holding 0 W.

## [0.8] — 2026-07/08 (untagged)
- Two-level SOC floor (`QW_MFRR_MIN_SOC`), idle-refresh keyed on command
  silence and raised to 96 h, zombie-subscription watchdog, connect retry,
  mFRR event cap raised to 7200 s, log audit with command-silence check,
  aFRR probe + durable WORKMODE capture, local bridge for Node-RED coexistence.

## [0.1] — 2026-06-07
- Cerbo-only Qilowatt mFRR agent: qilowatt-py link, dbus telemetry, Python
  state machine, DESS toggle + grid setpoint actuators, DESS watchdog.
