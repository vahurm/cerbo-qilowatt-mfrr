# Deploy

`install.sh` deploys everything to a Cerbo GX over SSH from your workstation.

```sh
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh                    # deploy, keep old agent running
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --dry-run-window 120  # deploy + 2 min dry-run of the new code
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --restart          # deploy + restart the agent
CERBO_HOST=root@<cerbo-ip> SSH_KEY=~/.ssh/<key> ./deploy/install.sh
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh --uninstall [--purge]
```

| Option | Effect |
|--------|--------|
| `--restart` | `svc -d` / `svc -u` the agent after deploying and print `svstat` |
| `--dry-run-window N` | stop the service, run the **new** agent for N s with `QW_DRY_RUN=1` (nothing actuated, intentions logged), print that log, restore the service |
| `--uninstall` | stop and remove the service, kill the loops, strip the `rc.local` hooks, return setpoint 0 / DESS on, delete the `/data` files. Keeps `/data/qw-agent.env` and the capture log |
| `--purge` | with `--uninstall`: delete `/data/qw-agent.env` too |
| `--pylib-tarball F` | use a prebuilt `pylib.tar.gz` (from `make release` or a GitHub release) instead of running pip here |
| `--no-doctor` | skip the self-check at the end |

What it does (idempotent):

1. Builds the pure-Python deps locally into `build/pylib` (arch-independent — no
   pip/internet needed on the Cerbo), or unpacks `--pylib-tarball`.
2. Copies the actuator, watchdog, audit and doctor scripts to `/data`, `chmod 750`.
3. Copies the agent + vendored libs to `/data/qw-agent`, the read-only
   diagnostics (`afrr_probe.py`, `afrr_capture.sh`), and writes
   `/data/qw-agent/VERSION` (`<__version__>+<git sha> <utc time>`), which the
   agent logs at start-up.
4. `/data/qw-agent.env`: left untouched if present. If missing and you are at a
   terminal it asks for the inverter id, MQTT user/password and the import/export
   caps and writes the file (`chmod 600`) from `.env.example`; non-interactive
   runs seed the example for you to edit.
5. Installs the daemontools service under `/data/qw-agent/service` and links it
   into `/service` (the rootfs copy is wiped by firmware updates; the link is
   recreated from `/data/rc.local` on boot).
6. Adds (once) to `/data/rc.local` and starts if not running: the DESS watchdog
   loop, the WORKMODE capture, and the hourly log audit loop.
7. Optional dry-run window and/or restart (see options).
8. Runs `/data/qw_doctor.sh` and prints its PASS/WARN/FAIL lines.

Without `--restart` or `--dry-run-window` the running agent keeps its **old**
code; restart when ready:

```sh
ssh root@<cerbo-ip> 'svc -d /service/qw-agent; svc -u /service/qw-agent'
ssh root@<cerbo-ip> 'svstat /service/qw-agent; tail -n 5 /var/log/qw-agent/current'
```

For an upgrade that changes actuation policy use `--dry-run-window` first
(single-client rule: the service is stopped for the window) and read
[`../docs/SAFETY.md`](../docs/SAFETY.md) before enabling live events. New
`QW_*` knobs are never written into an existing env file — add them by hand;
the agent's `CONFIG WARN` lines and the doctor tell you which ones matter.
