# Deploy

`install.sh` deploys everything to a Cerbo GX over SSH from your workstation.

```sh
CERBO_HOST=root@<cerbo-ip> ./deploy/install.sh
# or with an explicit key:
CERBO_HOST=root@<cerbo-ip> SSH_KEY=~/.ssh/cerbo_ed25519 ./deploy/install.sh
```

What it does (idempotent):

1. Builds the pure-Python deps locally into `build/pylib` (arch-independent — no
   pip/internet needed on the Cerbo).
2. Copies the actuator scripts to `/data` and `chmod 750`.
3. Copies the agent + vendored libs to `/data/qw-agent`.
4. Seeds `/data/qw-agent.env` from `.env.example` **only if missing** (never
   overwrites your secrets).
5. Installs the daemontools service at `/service/qw-agent`.
6. Adds (once) the watchdog loop to `/data/rc.local` and starts it.

After running, edit `/data/qw-agent.env` with the real Qilowatt `device_id` +
MQTT credentials, then restart the service:

```sh
ssh root@<cerbo-ip> 'svc -d /service/qw-agent; svc -u /service/qw-agent'
```

See [`../docs/INSTALL.md`](../docs/INSTALL.md) for manual steps and verification,
and [`../docs/SAFETY.md`](../docs/SAFETY.md) before enabling live events.
