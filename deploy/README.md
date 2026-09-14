# Deploy

`install.sh` deploys everything to a Cerbo GX over SSH from your workstation.

```sh
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh
# or with an explicit key:
CERBO_HOST=root@<cerbo-ip> SSH_KEY=~/.ssh/<key> ./deploy/install.sh
```

What it does (idempotent):

1. Builds the pure-Python deps locally into `build/pylib` (arch-independent — no
   pip/internet needed on the Cerbo).
2. Copies the actuator + audit scripts to `/data` and `chmod 750`.
3. Copies the agent + vendored libs to `/data/qw-agent`, and the read-only
   diagnostics (`afrr_probe.py`, `afrr_capture.sh`).
4. Seeds `/data/qw-agent.env` from `.env.example` **only if missing** (never
   overwrites your secrets — new variables you want to set go in by hand).
5. Installs the daemontools service under `/data/qw-agent/service` and links it
   into `/service` (the rootfs copy is wiped by firmware updates; the link is
   recreated from `/data/rc.local` on boot).
6. Adds (once) to `/data/rc.local` and starts if not running: the DESS watchdog
   loop, the WORKMODE capture, and the hourly log audit loop.

It does **not** restart a running agent, so the old code keeps running until you
do. After editing `/data/qw-agent.env` (credentials on a first install; new
`QW_*` knobs on an upgrade):

```sh
ssh root@<cerbo-ip> 'svc -d /service/qw-agent; svc -u /service/qw-agent'
ssh root@<cerbo-ip> 'svstat /service/qw-agent; tail -n 5 /var/log/qw-agent/current'
```

For an upgrade that changes actuation policy, do a short `QW_DRY_RUN=1` window
first with the service stopped (single-client rule) — see
[`../docs/INSTALL.md`](../docs/INSTALL.md#5-validate-in-dry-run-no-dbus-writes) —
and [`../docs/SAFETY.md`](../docs/SAFETY.md) before enabling live events.
