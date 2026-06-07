# Service (daemontools)

Venus OS supervises services in `/service` with daemontools. **`/service` is on
the rootfs and is wiped by Venus OS firmware updates**, so install the service
under `/data` (which persists) and symlink it into `/service`, and recreate that
symlink on every boot from `/data/rc.local`. Otherwise the agent silently stops
after the next firmware update.

```sh
# persistent copy under /data
mkdir -p /data/qw-agent/service
cp -r service/qw-agent /data/qw-agent/service/qw-agent
chmod 755 /data/qw-agent/service/qw-agent/run /data/qw-agent/service/qw-agent/log/run

# link into /service (svscan picks it up within ~5 s)
ln -sfn /data/qw-agent/service/qw-agent /service/qw-agent
svstat /service/qw-agent     # should show 'up' within ~5 s
```

Make the symlink survive firmware updates by adding this to `/data/rc.local`
(Venus OS runs it on boot; `deploy/install.sh` adds it for you):

```sh
# /var/log is tmpfs (wiped each boot); multilog needs the dir to exist
mkdir -p /var/log/qw-agent
if [ "$(readlink /service/qw-agent 2>/dev/null)" != /data/qw-agent/service/qw-agent ]; then
  rm -rf /service/qw-agent
  ln -s /data/qw-agent/service/qw-agent /service/qw-agent
fi
```

Control:

```sh
svc -d /service/qw-agent     # stop
svc -u /service/qw-agent     # start
tail -F /var/log/qw-agent/current
```

`run` expects the agent at `/data/qw-agent/qw_agent.py`, vendored deps at
`/data/qw-agent/pylib`, and config at `/data/qw-agent.env` (override with
`QW_AGENT_ENV`). See [`../docs/INSTALL.md`](../docs/INSTALL.md).
