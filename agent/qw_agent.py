#!/usr/bin/env python3
"""qw_agent.py — Cerbo-only Qilowatt mFRR agent.

Owns the Qilowatt cloud link (via the official `qilowatt-py` library) and runs
the whole mFRR loop on the Cerbo, with no Home Assistant in the path:

  * receives WORKMODE backlog commands from the Qilowatt MQTT broker;
  * drives a headless Python state machine (mfrr_statemachine.py) that toggles
    DESS and writes the grid setpoint via the /data/qw_*.sh actuators;
  * reports telemetry read from the Victron dbus (telemetry/ profiles);
  * optionally republishes the decoded WORKMODE to a local MQTT broker
    (QW_LOCAL_BRIDGE=1) for a Node-RED flow / dashboards. Off by default — the
    pure-Python path needs neither Node-RED nor a local broker.

No credentials are hard-coded. Configuration comes from environment variables,
typically loaded from an untracked /data/qw-agent.env (see .env.example).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import Dict, Optional

import paho.mqtt.client as mqtt
from qilowatt import InverterDevice, QilowattMQTTClient, WorkModeCommand

from actuators import DryRunActuator, ScriptActuator
from mfrr_statemachine import MfrrController
from telemetry import DbusReader, get_profile

_logger = logging.getLogger("qw_agent")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _load_env_file(path: str) -> None:
    """Load KEY=VALUE lines from a file into os.environ (does not override)."""
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val or val.startswith("REPLACE_WITH"):
        _logger.error("Missing required config: %s", name)
        raise SystemExit(2)
    return val


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0") == "1"


class Config:
    def __init__(self) -> None:
        # Qilowatt cloud
        self.device_id = _require("QW_DEVICE_ID")
        self.mqtt_user = _require("QW_MQTT_USER")
        self.mqtt_pass = _require("QW_MQTT_PASS")
        self.mqtt_host = os.environ.get("QW_MQTT_HOST", "mqtt.qilowatt.it")
        self.mqtt_port = int(os.environ.get("QW_MQTT_PORT", "8883"))
        self.mqtt_tls = _env_bool("QW_MQTT_TLS", True)

        # Local MQTT bridge (optional — for Node-RED / dashboards)
        self.local_bridge = _env_bool("QW_LOCAL_BRIDGE", False)
        self.local_host = os.environ.get("LOCAL_MQTT_HOST", "127.0.0.1")
        self.local_port = int(os.environ.get("LOCAL_MQTT_PORT", "1883"))
        self.local_user = os.environ.get("LOCAL_MQTT_USER", "") or None
        self.local_pass = os.environ.get("LOCAL_MQTT_PASS", "") or None
        self.prefix = os.environ.get("QW_LOCAL_PREFIX", "qw").rstrip("/")

        # mFRR state machine / actuators
        self.dry_run = _env_bool("QW_DRY_RUN", False)
        self.dess_script = os.environ.get("QW_DESS_TOGGLE_SCRIPT", "/data/qw_dess_toggle.sh")
        self.setpoint_script = os.environ.get("QW_GRID_SETPOINT_SCRIPT", "/data/qw_grid_setpoint.sh")
        self.mfrr_sources = tuple(
            s.strip().lower()
            for s in os.environ.get("QW_MFRR_SOURCES", "fusebox,kratt").split(",")
            if s.strip()
        )
        self.mqtt_lost_failsafe_s = float(os.environ.get("QW_MQTT_LOST_FAILSAFE_S", "300"))
        self.max_event_s = float(os.environ.get("QW_MAX_EVENT_S", "1800"))
        self.dess_off_delay_s = float(os.environ.get("QW_DESS_OFF_DELAY_S", "2"))
        self.tick_interval_s = float(os.environ.get("QW_TICK_INTERVAL_S", "10"))
        # Liveness watchdog: if the QW cloud link stays down this long, exit so
        # the service supervisor restarts us. qilowatt-py auto-reconnects
        # transient drops, but after repeated auth failures it shuts down for
        # good and never reconnects while our process keeps running — the
        # supervisor cannot help unless we actually exit. 0 disables.
        self.link_restart_s = float(os.environ.get("QW_LINK_RESTART_S", "600"))
        # Zombie-subscription watchdog: qilowatt-py explicitly keeps a session
        # marked "connected" even when the WORKMODE re-subscribe fails ("still
        # mark as connected so publishing works, just won't receive commands").
        # In that state telemetry keeps flowing but we go permanently deaf to
        # commands while our connection callback reports healthy — the link
        # failsafe above never fires. If the transport is connected but the
        # command topic is not subscribed for this long, exit for a fresh
        # restart. 0 disables.
        self.subscribe_grace_s = float(os.environ.get("QW_SUBSCRIBE_GRACE_S", "120"))
        # Backstop for silently deaf sessions that still report subscribed
        # (half-dead socket the broker/keepalive never tears down): if NO WORKMODE
        # command is received for this long while IDLE, exit for a fresh
        # subscription. A received command (incl. periodic NORMAL heartbeats)
        # proves the subscription is live, so a site that keeps getting commands
        # never restarts; only genuine command-silence triggers a refresh. Must be
        # comfortably larger than the longest expected gap between commands. An
        # in-process reconnect cannot help — qilowatt-py starts its telemetry
        # timers exactly once — so a clean process restart is the only safe
        # refresh. 0 disables.
        self.idle_refresh_s = float(os.environ.get("QW_IDLE_REFRESH_S", "21600"))

        # Telemetry
        self.telemetry_profile = os.environ.get("QW_TELEMETRY_PROFILE", "dc_coupled")
        self.export_limit_w = float(os.environ.get("QW_GRID_EXPORT_LIMIT_W", "15000"))
        self.telemetry_interval_s = float(os.environ.get("QW_TELEMETRY_INTERVAL_S", "5"))

        self.log_level = os.environ.get("QW_LOG_LEVEL", "INFO").upper()


# --------------------------------------------------------------------------- #
# Local MQTT bridge (optional)
# --------------------------------------------------------------------------- #

class LocalBridge:
    """Publishes decoded WORKMODE + connection state to a local broker.

    Optional — only started when QW_LOCAL_BRIDGE=1 (e.g. to feed a Node-RED flow
    or a dashboard). The pure-Python path does not need it.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._online_topic = f"{cfg.prefix}/online"
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="qw-agent-local")
        if cfg.local_user:
            client.username_pw_set(cfg.local_user, cfg.local_pass)
        client.will_set(self._online_topic, payload="false", qos=0, retain=True)
        client.reconnect_delay_set(min_delay=2, max_delay=30)
        self._client = client

    def start(self) -> None:
        self._client.connect_async(self._cfg.local_host, self._cfg.local_port, keepalive=30)
        self._client.loop_start()
        self._client.publish(self._online_topic, "true", qos=0, retain=True)

    def stop(self) -> None:
        try:
            self._client.publish(self._online_topic, "false", qos=0, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def _pub(self, suffix: str, payload: str) -> None:
        topic = f"{self._cfg.prefix}/{suffix}"
        self._client.publish(topic, payload, qos=0, retain=True)
        _logger.debug("local publish %s = %s", topic, payload)

    def publish_workmode(self, command: WorkModeCommand) -> None:
        data: Dict = command.to_dict()
        source = data.get("_source", "") or ""
        mode = data.get("Mode", "normal") or "normal"
        power = data.get("PowerLimit", 0)
        self._pub("qw_source", str(source))
        self._pub("qw_mode", str(mode))
        self._pub("qw_powerlimit", str(int(power) if power is not None else 0))

    def publish_connected(self, connected: bool) -> None:
        self._pub("qw_connected", "on" if connected else "off")


# --------------------------------------------------------------------------- #
# Telemetry loop
# --------------------------------------------------------------------------- #

class TelemetryLoop(threading.Thread):
    def __init__(self, cfg: Config, device: InverterDevice, profile) -> None:
        super().__init__(name="TelemetryLoop", daemon=True)
        self._cfg = cfg
        self._device = device
        self._profile = profile
        self._reader = DbusReader()
        self._stop = threading.Event()
        if not self._reader.available:
            _logger.warning("dbus not available — telemetry will report zeros")

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                energy = self._profile.build_energy_data(self._reader, self._cfg.export_limit_w)
                metrics = self._profile.build_metrics_data(self._reader, self._cfg.export_limit_w)
                self._device.set_energy_data(energy)
                self._device.set_metrics_data(metrics)
            except Exception as exc:
                _logger.error("telemetry update failed: %s", exc)
            self._stop.wait(self._cfg.telemetry_interval_s)

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Failsafe tick
# --------------------------------------------------------------------------- #

class TickThread(threading.Thread):
    def __init__(self, controller: MfrrController, interval_s: float) -> None:
        super().__init__(name="MfrrTick", daemon=True)
        self._controller = controller
        self._interval = interval_s
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._controller.tick()
            except Exception as exc:
                _logger.error("failsafe tick error: %s", exc)

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Link liveness
# --------------------------------------------------------------------------- #

class LinkMonitor:
    """Tracks how long the QW cloud link has been continuously down.

    qilowatt-py reconnects transient drops on its own, but after repeated auth
    failures it shuts down permanently and never reconnects — while our process
    stays alive, so a supervisor cannot recover us. main() polls ``down_for()``
    and exits when it exceeds ``QW_LINK_RESTART_S`` so daemontools restarts the
    agent with a fresh connection. Startup counts as "down" until the first
    successful connect.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connected = False
        self._down_since: Optional[float] = time.monotonic()

    def set(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected
            if connected:
                self._down_since = None
            elif self._down_since is None:
                self._down_since = time.monotonic()

    def down_for(self) -> float:
        """Seconds the link has been continuously down (0.0 while connected)."""
        with self._lock:
            if self._connected or self._down_since is None:
                return 0.0
            return time.monotonic() - self._down_since


class ConnectionWatchdog:
    """Decides when the agent must exit for a clean supervisor restart.

    qilowatt-py's connection callback is not a trustworthy liveness signal:

      * after a failed WORKMODE re-subscribe it *still* reports "connected"
        (its own comment: "still mark as connected so publishing works ... just
        won't receive commands"), so ``LinkMonitor`` sees a healthy link while
        we are permanently deaf to commands;
      * a half-dead socket the broker/keepalive never tears down can keep the
        session "connected" *and* "subscribed" yet deliver nothing.

    An in-process reconnect cannot recover either case: qilowatt-py starts its
    telemetry timers exactly once (``_data_initialized`` latches True), so
    ``disconnect()``/``connect()`` would silence telemetry forever. The only
    clean recovery is to exit so daemontools starts a fresh process. This
    watchdog folds together three exit triggers and returns a human-readable
    reason (or ``None``):

      1. link reported down longer than ``link_restart_s`` (qilowatt-py gave up);
      2. transport connected but command topic unsubscribed > ``subscribe_grace_s``;
      3. no WORKMODE command received for >= ``idle_refresh_s`` while IDLE. A
         received command (including periodic NORMAL heartbeats) proves the
         subscription is live, so this only fires after genuine command-silence
         long enough to suspect a silently deaf session; an active mFRR event is
         never interrupted.
    """

    def __init__(
        self,
        link_restart_s: float,
        subscribe_grace_s: float,
        idle_refresh_s: float,
    ) -> None:
        self._link_restart_s = link_restart_s
        self._subscribe_grace_s = subscribe_grace_s
        self._idle_refresh_s = idle_refresh_s
        self._start = time.monotonic()
        self._sub_bad_since: Optional[float] = None
        self._last_command_at = self._start

    def note_command(self) -> None:
        """Record that a WORKMODE command was just received."""
        self._last_command_at = time.monotonic()

    def check(
        self,
        transport_connected: bool,
        subscribed: bool,
        link_down_s: float,
        state: str,
    ) -> Optional[str]:
        """Return a restart reason if the agent should exit, else ``None``."""
        now = time.monotonic()

        if self._link_restart_s > 0 and link_down_s >= self._link_restart_s:
            return (
                f"QW link down for {link_down_s:.0f}s (>= {self._link_restart_s:.0f}s); "
                "qilowatt-py may have given up permanently"
            )

        if self._subscribe_grace_s > 0:
            if transport_connected and not subscribed:
                if self._sub_bad_since is None:
                    self._sub_bad_since = now
                elif now - self._sub_bad_since >= self._subscribe_grace_s:
                    return (
                        "QW transport connected but command subscription dead for "
                        f"{now - self._sub_bad_since:.0f}s (>= {self._subscribe_grace_s:.0f}s); "
                        "deaf to WORKMODE"
                    )
            else:
                self._sub_bad_since = None

        if (
            self._idle_refresh_s > 0
            and state == "IDLE"
            and now - self._last_command_at >= self._idle_refresh_s
        ):
            return (
                f"no WORKMODE command received for {now - self._last_command_at:.0f}s "
                f"(>= {self._idle_refresh_s:.0f}s) while IDLE; refreshing the session "
                "in case it is silently deaf"
            )

        return None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    _load_env_file(os.environ.get("QW_AGENT_ENV", "/data/qw-agent.env"))
    cfg = Config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _logger.info(
        "Starting qw_agent device=%s profile=%s dry_run=%s local_bridge=%s",
        cfg.device_id, cfg.telemetry_profile, cfg.dry_run, cfg.local_bridge,
    )

    profile = get_profile(cfg.telemetry_profile)

    actuator = DryRunActuator() if cfg.dry_run else ScriptActuator(
        cfg.dess_script, cfg.setpoint_script
    )
    controller = MfrrController(
        actuator,
        mfrr_sources=cfg.mfrr_sources,
        mqtt_lost_failsafe_s=cfg.mqtt_lost_failsafe_s,
        max_duration_s=cfg.max_event_s,
        dess_off_delay_s=cfg.dess_off_delay_s,
    )

    # Fan command/connection events out to the controller (+ optional bridge).
    command_handlers = [controller.on_workmode]
    connection_handlers = [controller.on_connected]

    bridge = None
    if cfg.local_bridge:
        bridge = LocalBridge(cfg)
        bridge.start()
        command_handlers.append(bridge.publish_workmode)
        connection_handlers.append(bridge.publish_connected)

    device = InverterDevice(device_id=cfg.device_id)

    watchdog = ConnectionWatchdog(
        link_restart_s=cfg.link_restart_s,
        subscribe_grace_s=cfg.subscribe_grace_s,
        idle_refresh_s=cfg.idle_refresh_s,
    )

    def on_command(command: WorkModeCommand) -> None:
        _logger.info("WORKMODE received: %s", command.to_dict())
        watchdog.note_command()
        for handler in command_handlers:
            try:
                handler(command)
            except Exception as exc:
                _logger.error("command handler %s error: %s", handler, exc)

    device.set_command_callback(on_command)

    client = QilowattMQTTClient(
        mqtt_username=cfg.mqtt_user,
        mqtt_password=cfg.mqtt_pass,
        device=device,
        host=cfg.mqtt_host,
        port=cfg.mqtt_port,
        tls=cfg.mqtt_tls,
    )

    telemetry = TelemetryLoop(cfg, device, profile)
    # Start telemetry only once the link is up. Setting the first energy+metrics
    # data triggers qilowatt-py's start_timers(), which publishes STATUS0
    # immediately; doing that before the MQTT connection is established loses the
    # device-announcement STATUS0 for up to an hour (its refresh period).
    telemetry_started = threading.Event()
    link = LinkMonitor()

    def on_connection(connected: bool) -> None:
        _logger.info("QW connection state: %s", "connected" if connected else "disconnected")
        link.set(connected)
        if connected and not telemetry_started.is_set():
            telemetry_started.set()
            telemetry.start()
        for handler in connection_handlers:
            try:
                handler(connected)
            except Exception as exc:
                _logger.error("connection handler %s error: %s", handler, exc)

    client.add_connection_callback(on_connection)

    tick = TickThread(controller, cfg.tick_interval_s)
    tick.start()

    client.connect()

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        _logger.info("Received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    exit_code = 0
    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
            reason = watchdog.check(
                transport_connected=client.transport_connected,
                subscribed=client.subscribed,
                link_down_s=link.down_for(),
                state=controller.state,
            )
            if reason:
                _logger.error(
                    "%s; exiting so the supervisor restarts the agent with a "
                    "fresh QW connection.",
                    reason,
                )
                exit_code = 3
                break
    finally:
        _logger.info("Stopping qw_agent")
        tick.stop()
        telemetry.stop()
        # Leave the system in a safe state if an event was active.
        controller.shutdown()
        try:
            client.disconnect()
        except Exception:
            pass
        if bridge is not None:
            bridge.publish_connected(False)
            bridge.stop()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
