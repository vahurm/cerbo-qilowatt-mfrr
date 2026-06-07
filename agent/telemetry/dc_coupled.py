"""DC-coupled PV telemetry profile (Victron MPPT) — e.g. site A.

PV is mainly wired to the battery DC bus via Victron MPPT chargers, so the DC PV
total is `/Dc/Pv/Power` on com.victronenergy.system (the same quantity as Modbus
register 850). Site A also has a small AC-coupled `pvinverter` service, so total
PV = DC PV + pvinverter /Ac/Power (clamped at >= 0; the inverter reads a small
negative self-consumption at night).
"""

from __future__ import annotations

from . import base


def _pv_power(reader: base.DbusReader) -> float:
    dc = reader.get_float(base.SVC_SYSTEM, "/Dc/Pv/Power")
    ac = 0.0
    pv_service = reader.find_service(base.SVC_PVINVERTER_PREFIX)
    if pv_service:
        ac = reader.get_float(pv_service, "/Ac/Power")
    total = dc + ac
    return total if total > 0 else 0.0


def build_energy_data(reader: base.DbusReader, grid_export_limit_w: float):
    return base.build_energy_data(reader, grid_export_limit_w)


def build_metrics_data(reader: base.DbusReader, grid_export_limit_w: float):
    return base.build_metrics_data(reader, grid_export_limit_w, _pv_power)
