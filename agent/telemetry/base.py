"""Base dbus reader + shared telemetry builders for the Qilowatt agent.

Site topology differences (AC- vs DC-coupled PV) live in the profile modules
(telemetry/dc_coupled.py, telemetry/ac_coupled.py). This module holds the parts
that are identical across topologies: the dbus access wrapper, the grid-side
ENERGY block, and a parametrised METRICS builder where only the PV-power source
varies between profiles.

Design notes:
  * Uses the system-provided `python3-dbus` (present on Venus OS). If dbus is not
    available (e.g. running on a dev laptop), it degrades to returning zeros so
    the daemon still imports and runs for development.
  * The exact set of fields Qilowatt expects must be validated against the live
    `SENSOR` payload for the specific system before relying on market
    settlement — see the `VALIDATE` comments.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

try:
    import dbus  # python3-dbus, system package on Venus OS
    _DBUS_AVAILABLE = True
except Exception:  # pragma: no cover - dev machines without dbus
    dbus = None
    _DBUS_AVAILABLE = False

from qilowatt import EnergyData, MetricsData

_logger = logging.getLogger(__name__)

# Victron service names. On a Cerbo these are well-known bus names.
SVC_SYSTEM = "com.victronenergy.system"
SVC_SETTINGS = "com.victronenergy.settings"

# Dynamic service names (contain a serial); discovered at runtime by prefix.
SVC_VEBUS_PREFIX = "com.victronenergy.vebus"
SVC_PVINVERTER_PREFIX = "com.victronenergy.pvinverter"

# Dynamic ESS / export-limit setting used for the GridExportLimit metric.
PATH_MAX_FEEDIN = "/Settings/CGwacs/MaxFeedInPower"

# A telemetry profile only needs to supply a PV-power reader.
PvPowerFn = Callable[["DbusReader"], float]


class DbusReader:
    """Thin wrapper around the Venus system bus with safe GetValue access."""

    def __init__(self) -> None:
        self._bus = None
        self._svc_cache: dict = {}
        if _DBUS_AVAILABLE:
            try:
                self._bus = dbus.SystemBus()
            except Exception as exc:  # pragma: no cover
                _logger.warning("Could not connect to system dbus: %s", exc)
                self._bus = None

    @property
    def available(self) -> bool:
        return self._bus is not None

    def find_service(self, prefix: str):
        """Return the first bus name starting with `prefix` (cached), or None.

        Used for services whose name carries a serial number
        (vebus.ttyS4, pvinverter.cg_..., grid.ve_...).
        """
        if self._bus is None:
            return None
        if prefix in self._svc_cache:
            return self._svc_cache[prefix]
        found = None
        try:
            for name in self._bus.list_names():
                s = str(name)
                if s.startswith(prefix):
                    found = s
                    break
        except Exception:
            found = None
        self._svc_cache[prefix] = found
        return found

    def get(self, service: str, path: str, default=None):
        """Read a single dbus BusItem value; return `default` on any failure."""
        if self._bus is None:
            return default
        try:
            obj = self._bus.get_object(service, path)
            value = obj.GetValue(dbus_interface="com.victronenergy.BusItem")
            return _to_native(value)
        except Exception:
            # Missing paths are normal across firmware/hardware variants.
            return default

    def get_float(self, service: str, path: str, default: float = 0.0) -> float:
        v = self.get(service, path, None)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def get_int(self, service: str, path: str, default: int = 0) -> int:
        return int(round(self.get_float(service, path, float(default))))


def _to_native(value):
    """Convert dbus typed values (incl. empty-array 'invalid') to Python."""
    if value is None:
        return None
    # dbus arrays are used as the 'invalid'/unavailable sentinel on Venus.
    if isinstance(value, (list, tuple)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def phases_float(reader: DbusReader, service: str, template: str) -> List[float]:
    """Read a 3-phase set like /Ac/Grid/L{n}/Power -> [l1, l2, l3]."""
    return [reader.get_float(service, template.format(n=n)) for n in (1, 2, 3)]


def z(value: Optional[float]) -> float:
    return 0.0 if value is None else round(float(value), 1)


def rnd(value: Optional[float], default: float) -> float:
    return default if value is None else round(float(value), 1)


def volt(value: Optional[float]) -> float:
    """Grid voltage with a nominal fallback for missing/implausible readings."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 230.0
    return round(v, 1) if v >= 50.0 else 230.0


def build_energy_data(reader: DbusReader, grid_export_limit_w: float) -> EnergyData:
    """ENERGY block: grid-side AC measurements (per phase) + totals.

    Identical across topologies — the grid meter is the grid meter regardless of
    where PV is coupled. /Ac/Grid/L{n}/Power on com.victronenergy.system is
    confirmed in use by site B's curtailment flow.

    VALIDATE: Qilowatt's ENERGY.Power sign convention and Today/Total source
    against the live SENSOR payload for the target system.

    Grid voltage/frequency are NOT on com.victronenergy.system on all systems
    (site A raised DBusException). They are read from the vebus AC-input instead
    (/Ac/ActiveIn/L{n}/V, /Ac/ActiveIn/L1/F), which carries the real grid-side
    measurement, with a nominal fallback.
    """
    power = phases_float(reader, SVC_SYSTEM, "/Ac/Grid/L{n}/Power")
    current = phases_float(reader, SVC_SYSTEM, "/Ac/Grid/L{n}/Current")

    vebus = reader.find_service(SVC_VEBUS_PREFIX)
    if vebus:
        voltage = phases_float(reader, vebus, "/Ac/ActiveIn/L{n}/V")
        f = reader.get_float(vebus, "/Ac/ActiveIn/L1/F", 50.0)
        frequency = round(f, 2) if f >= 40.0 else 50.0
    else:
        voltage = phases_float(reader, SVC_SYSTEM, "/Ac/Grid/L{n}/Voltage")
        frequency = 50.0

    return EnergyData(
        Power=[z(p) for p in power],
        Today=0.0,   # VALIDATE: map to grid/inverter daily energy if required
        Total=0.0,   # VALIDATE: map to lifetime energy if required
        Current=[z(c) for c in current],
        Voltage=[volt(v) for v in voltage],
        Frequency=frequency,
    )


def build_metrics_data(
    reader: DbusReader, grid_export_limit_w: float, pv_power_fn: PvPowerFn
) -> MetricsData:
    """METRICS block: PV / load / battery / limits / temperatures.

    Only `pv_power_fn` varies between profiles (AC- vs DC-coupled PV). Battery,
    load and limits live on com.victronenergy.system and are topology-agnostic.
    Paths that a given system does not expose return None (-> 0.0), which is
    harmless.
    """
    pv_power = pv_power_fn(reader)
    load_power = phases_float(reader, SVC_SYSTEM, "/Ac/Consumption/L{n}/Power")

    batt_power = reader.get_float(SVC_SYSTEM, "/Dc/Battery/Power")
    batt_voltage = reader.get_float(SVC_SYSTEM, "/Dc/Battery/Voltage")
    batt_current = reader.get_float(SVC_SYSTEM, "/Dc/Battery/Current")
    batt_soc = reader.get_int(SVC_SYSTEM, "/Dc/Battery/Soc")
    batt_temp = reader.get_float(SVC_SYSTEM, "/Dc/Battery/Temperature")

    # MaxFeedInPower: -1 means "unlimited"; report the configured cap instead.
    max_feedin = reader.get_float(SVC_SETTINGS, PATH_MAX_FEEDIN, -1.0)
    export_limit = (
        grid_export_limit_w if max_feedin is None or max_feedin < 0 else max_feedin
    )

    return MetricsData(
        PvPower=[z(pv_power)],
        PvVoltage=[0.0],          # VALIDATE: per-MPPT voltage if available
        PvCurrent=[0.0],          # VALIDATE: per-MPPT current if available
        LoadPower=[z(p) for p in load_power],
        BatterySOC=int(batt_soc),
        LoadCurrent=[0.0, 0.0, 0.0],
        BatteryPower=[z(batt_power)],
        BatteryCurrent=[z(batt_current)],
        BatteryVoltage=[rnd(batt_voltage, 0.0)],
        GenVoltage=[0.0, 0.0, 0.0],
        GenPower=[0.0, 0.0, 0.0],
        GenCurrent=[0.0, 0.0, 0.0],
        GridExportLimit=float(export_limit),
        BatteryTemperature=[z(batt_temp)],
        # Inverter status/alarms/temperature mirror the official qilowatt-ha
        # VictronInverter payload: a Victron ESS exposes no single inverter
        # "running status" register (unlike Deye/Sunsynk inverters the Qilowatt
        # enum was built for), so the reference sends a constant status 2 and
        # all-zero alarms, and reports the battery temperature as the inverter
        # temperature. The dashboard shows this status as "Unknown" for Victron
        # — that is expected, not a fault.
        InverterStatus=2,
        AlarmCodes=[0, 0, 0, 0, 0, 0],
        InverterTemperature=z(batt_temp),
    )
