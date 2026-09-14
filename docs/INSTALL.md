# Install

Target: Victron Cerbo GX running **Venus OS**. SSH access required.

The agent runs the whole mFRR loop in Python — **Node-RED and Venus OS Large are
NOT required**. Use the optional [Node-RED flow](#7-optional-node-red-flow) only
if you want a co-resident curtailment flow or a dashboard.

> Read [SAFETY.md](SAFETY.md) first. Set `QW_MAX_IMPORT_W` / `QW_MAX_EXPORT_W` to
> your site's grid-connection import capacity and feed-in (export) cap.

## Prerequisites

- Venus OS installed (standard image is fine; Large only needed for Node-RED).
- SSH enabled (Settings → General → SSH on LAN) and a root password set.
- A Qilowatt device with `device_id` + MQTT username/password (from Qilowatt
  support), and mFRR market access for the site.
- Your grid connection's import and export limits in W (`QW_MAX_IMPORT_W` /
  `QW_MAX_EXPORT_W`). The agent warns at start-up while they are unset or at
  the example value; `qw_doctor.sh` suggests a figure from the AC input limit.
- PV topology no longer matters for telemetry: the default `auto` profile sums
  DC MPPTs and PV inverters on both the output and the grid side.
- The README's "What your site must have" table lists the dbus paths and ESS
  settings that must be present; `qw_doctor.sh` checks every one of them.

> **One client per device.** Qilowatt allows a single active MQTT client per
> `device_id`. If a Home Assistant `qilowatt-ha` integration currently uses these
> credentials, retire it before starting the agent here. Run the agent in
> `QW_DRY_RUN=1` first to validate without contending — but the cloud link still
> consumes the single client slot, so do the dry-run in a brief window with
> `qilowatt-ha` (or a previously running agent) stopped.

## 0. The short way

[`deploy/install.sh`](../deploy/install.sh) does steps 1–3 and 6 (plus the
diagnostics, the hourly log audit and the `VERSION` stamp) in one idempotent
run from your workstation, asks for the four per-site values if no env exists
(step 4), can run the dry-run window (step 5) and the restart for you, and ends
with the `qw_doctor.sh` self-check. The rest of this page is the manual
equivalent, for understanding what lands where.

```sh
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh                      # install, doctor
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --dry-run-window 120    # validate the new code
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --restart               # go live
```

See [`deploy/README.md`](../deploy/README.md) for all options (`--uninstall`,
`--pylib-tarball`, …).

## 1. Actuator scripts

```sh
scp scripts/qw_dess_toggle.sh   root@<cerbo-ip>:/data/
scp scripts/qw_grid_setpoint.sh root@<cerbo-ip>:/data/
scp scripts/qw_dess_watchdog.sh root@<cerbo-ip>:/data/
scp scripts/qw_log_audit.sh     root@<cerbo-ip>:/data/
ssh root@<cerbo-ip> 'chmod 750 /data/qw_dess_toggle.sh /data/qw_grid_setpoint.sh /data/qw_dess_watchdog.sh /data/qw_log_audit.sh'
```

Quick test (returns the system to normal afterwards):

```sh
ssh root@<cerbo-ip> '/data/qw_dess_toggle.sh status'
ssh root@<cerbo-ip> '/data/qw_grid_setpoint.sh get'
```

## 2. Watchdog boot loop

Add to `/data/rc.local` (create it `chmod 755` if missing):

```sh
# QW DESS watchdog
( while true; do /data/qw_dess_watchdog.sh; sleep 60; done ) &
```

Start it now without rebooting:

```sh
ssh root@<cerbo-ip> "nohup sh -c 'while true; do /data/qw_dess_watchdog.sh; sleep 60; done' >/dev/null 2>&1 &"
```

The same file should carry the hourly log audit loop
(`/data/qw_log_audit.sh`, see [SAFETY.md](SAFETY.md)) — `install.sh` adds both
and checks on every run that they are actually running, not merely present in
`rc.local`.

## 3. Python agent + dependencies

`dbus` access uses the system `python3-dbus` already on Venus OS. The pip
dependencies (`qilowatt`, `paho-mqtt`, `getmac`) must be **vendored** under
`/data` so they survive firmware updates.

Venus OS often has no `pip` on the device, so vendor the libs **on your
workstation** and copy them over:

```sh
# On your workstation (any machine with python3 + pip):
python3 -m pip install --target=./pylib -r agent/requirements.txt
scp -r agent          root@<cerbo-ip>:/data/qw-agent
scp -r pylib          root@<cerbo-ip>:/data/qw-agent/pylib
```

> If the device *does* have pip, you can instead run
> `python3 -m pip install --target=/data/qw-agent/pylib -r /data/qw-agent/requirements.txt`
> on the Cerbo.
>
> `getmac` is only used by `qilowatt-py` for a device fingerprint. If you cannot
> vendor it, drop a 3-line stub `getmac.py` exposing `get_mac_address()` into
> `/data/qw-agent/pylib/`.

Run the agent with the vendored libs on the path:

```sh
PYTHONPATH=/data/qw-agent/pylib QW_AGENT_ENV=/data/qw-agent.env \
  python3 /data/qw-agent/qw_agent.py
```

## 4. Per-site configuration (secrets stay off git)

```sh
scp .env.example root@<cerbo-ip>:/data/qw-agent.env
ssh root@<cerbo-ip> 'chmod 600 /data/qw-agent.env'
# edit /data/qw-agent.env: fill QW_DEVICE_ID / QW_MQTT_USER / QW_MQTT_PASS
# and the QW_MAX_IMPORT_W / QW_MAX_EXPORT_W limits.
```

Then decide the site-specific policy knobs, each explained in `.env.example`:

| Variable | Decide |
|---|---|
| `QW_MFRR_SOURCES` | which dispatchers to obey (default `fusebox,kratt,qilowatt`; never `optimizer`). A dispatch from a source not listed is logged as `dropping FRR dispatch` |
| `QW_MFRR_MIN_SOC` | how deep mFRR may discharge; must be *below* the dashboard Minimum SOC |
| `QW_TRADE_MODES` | honour Qilowatt's Q trades (`buy,sell`, default) or drop them (empty) |
| `QW_MAX_EVENT_S`, `QW_MAX_TRADE_S` | event caps; both must stay below the watchdog's `QW_MAX_OFF_SECS` |
| `QW_IDLE_REFRESH_S` | restart-on-silence backstop; keep above the site's real command silence or set 0 |
| `QW_ALERT_URL` | optional: where `qw_log_audit.sh` POSTs WARN/ERROR findings (ntfy, webhook) |
| `QW_STATE_FILE` | where the agent mirrors its state as JSON (`/data/qw-agent/state.json`); empty = off |

The agent re-checks the risky ones at every start and logs `CONFIG WARN: …`
for each finding (caps ordering, missing/example import-export caps,
`QW_MFRR_MIN_SOC` vs the live floor, trades disabled).

> **QW_DEVICE_ID gotcha:** this is the MQTT topic id (`Q/<id>/SENSOR`,
> `Q/<id>/cmnd/backlog`). When migrating off `qilowatt-ha`, use the config
> entry's **`inverter_id`** (a UUID), not the account-level `device_id` hex
> string. The wrong id leaves the dashboard empty *and* drops mFRR commands.

## 5. Validate in dry-run (no dbus writes)

The easy way is `./deploy/install.sh --dry-run-window 120`, which stops the
service, runs the new code for two minutes with `QW_DRY_RUN=1`, prints its log
and restores the service. By hand:

```sh
# qilowatt-ha / the service stopped during this window (single-client rule):
PYTHONPATH=/data/qw-agent/pylib QW_AGENT_ENV=/data/qw-agent.env \
  QW_DRY_RUN=1 python3 /data/qw-agent/qw_agent.py
```

To see what the agent would have done with *your* dispatch history before
that, replay the durable capture offline on any machine:

```sh
python3 tools/replay.py --log afrr-workmode.log --env qw-agent.env --timeline
```

Confirm in the logs that WORKMODE commands decode correctly (the portal pushes a
snapshot ~20 s after connect), telemetry reports sane PV/battery/grid values,
and that the startup line shows the policy you intended
(`mFRR sources=…; Q trades enabled/DISABLED …; limits import=… export=…`).
Any event during the window logs the intended `DESS off → setpoint →
setpoint 0 → DESS on` sequence as `[dry-run]` lines. Compare the telemetry
against VRM / the previous `qilowatt-ha` sensors before going live.

Stop the dry-run with SIGTERM to the agent's own pid — `pkill -f qw_agent.py`
from an SSH one-liner also matches the SSH shell that contains the same
string, and kills your session before it restarts the service.

## 6. Run as a service (daemontools)

See [`../service/qw-agent/`](../service/qw-agent). The easiest path is
[`deploy/install.sh`](../deploy/install.sh), which sets all of this up. To do it
by hand:

> **`/service` is on the rootfs and is WIPED by Venus OS firmware updates.**
> Install the service under `/data` (persistent), symlink it into `/service`,
> and recreate that symlink on boot from `/data/rc.local`. Skip this and the
> agent — and your Qilowatt telemetry — silently dies after the next firmware
> update (the topic shows "last seen …" with no obvious cause).

```sh
# persistent copy under /data + symlink into /service
mkdir -p /data/qw-agent/service
cp -r service/qw-agent /data/qw-agent/service/qw-agent
chmod 755 /data/qw-agent/service/qw-agent/run /data/qw-agent/service/qw-agent/log/run
ln -sfn /data/qw-agent/service/qw-agent /service/qw-agent
# the supervisor picks it up within ~5 s; check:
svstat /service/qw-agent

# recreate the symlink on every boot (survives firmware updates)
[ -f /data/rc.local ] || printf '#!/bin/sh\n' > /data/rc.local
grep -q 'qw-agent/service/qw-agent' /data/rc.local || cat >> /data/rc.local <<'EOF'
mkdir -p /var/log/qw-agent
if [ "$(readlink /service/qw-agent 2>/dev/null)" != /data/qw-agent/service/qw-agent ]; then
  rm -rf /service/qw-agent
  ln -s /data/qw-agent/service/qw-agent /service/qw-agent
fi
EOF
chmod 755 /data/rc.local
```

## 7. Optional: Node-RED as a read-only consumer

Only if you run Venus OS Large and want a co-resident curtailment flow or a
dashboard. Set `QW_LOCAL_BRIDGE=1` in the env so the agent republishes the
decoded WORKMODE and its event state to the local broker, then have your flow
subscribe to `qw/mfrr_active`, `qw/online` (and optionally `qw/mfrr_kind`) as
described in `nodered/curtailment-mfrr-aware.md`. Do **not** import the legacy
orchestrator in `contrib/nodered-legacy/` on a site where the agent runs.

## Verify end-to-end

In the Qilowatt portal the device should show **online**, with `STATUS0`/`SENSOR`
flowing. With `QW_LOCAL_BRIDGE=1` you can also watch the decoded values:

```sh
mosquitto_sub -h 127.0.0.1 -t 'qw/#' -v
```

On the Cerbo itself:

```sh
/data/qw_doctor.sh                             # PASS/WARN/FAIL self-check of the whole site
svstat /service/qw-agent                       # up, pid, uptime
tail -F /var/log/qw-agent/current              # mFRR/TRADE START/END lines
cat /data/qw-agent/state.json                  # state, kind, signed W, degraded, version
/data/qw_dess_toggle.sh status                 # DESS Mode + SOC floor, saved state
/data/qw_log_audit.sh                          # what changed since the last hour
python3 /data/qw-agent/afrr_probe.py --log /data/afrr-workmode.log   # stream classifier
```

The first log line after a restart is
`Starting qw_agent <version> (install <VERSION stamp>) …`; a `STARTUP RECOVERY`
line right after it means the previous process died mid-event and the agent
returned the site to normal before connecting. If something is off, go through
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

## Development: running the tests

The agent logic is covered by a `pytest` suite plus dependency-free POSIX-sh
tests for every shell script. They need no Cerbo, dbus, or network — the
actuators / telemetry / `dbus` CLI are driven through fakes. Run them on a
workstation:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r agent/requirements.txt -r requirements-dev.txt
make test                      # everything below
pytest -q                      # state machine, actuators (read-back), startup,
                               # telemetry, bridge contract, config, probe, replay
sh tests/test_grid_setpoint.sh # asymmetric import/export clamp
sh tests/test_dess_toggle.sh   # DESS + SOC-floor save/lower/restore, --no-floor
sh tests/test_log_audit.sh     # audit findings, incremental window, silence, alerts
sh tests/test_doctor.sh        # self-check verdicts against a stubbed site
make lint                      # shellcheck (POSIX sh) — pip install shellcheck-py
```

CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) runs all of them
on Python 3.8, 3.10 and 3.12 plus shellcheck on every push and pull request.

## Uninstall / rollback

```sh
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --uninstall          # keeps qw-agent.env
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --uninstall --purge  # removes it too
```

or by hand, minimum to stop actuating:

```sh
svc -d /service/qw-agent        # stop the agent (its shutdown reverts any active event)
/data/qw_dess_toggle.sh on
/data/qw_grid_setpoint.sh 0
```
