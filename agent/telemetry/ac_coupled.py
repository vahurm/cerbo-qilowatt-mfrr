"""AC-coupled PV telemetry profile (PV inverter on AC output) — e.g. site B.

PV is an AC-coupled inverter (Huawei/Fronius/etc.) on the MultiPlus output, so
the PV total is summed from `/Ac/PvOnOutput/L{n}/Power` on
com.victronenergy.system. This is the original mapping confirmed by site B's
curtailment flow.
"""

from __future__ import annotations

from . import base


def _pv_power(reader: base.DbusReader) -> float:
    return sum(base.phases_float(reader, base.SVC_SYSTEM, "/Ac/PvOnOutput/L{n}/Power"))


def build_energy_data(reader: base.DbusReader, grid_export_limit_w: float):
    return base.build_energy_data(reader, grid_export_limit_w)


def build_metrics_data(reader: base.DbusReader, grid_export_limit_w: float):
    return base.build_metrics_data(reader, grid_export_limit_w, _pv_power)
