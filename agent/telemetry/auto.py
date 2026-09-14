"""Topology-agnostic telemetry profile — the default.

Every PV source a Venus system knows about is aggregated on
``com.victronenergy.system``:

  * ``/Dc/Pv/Power``                 — Victron MPPTs (DC-coupled),
  * ``/Ac/PvOnOutput/L{n}/Power``    — PV inverters on the Multi's AC output,
  * ``/Ac/PvOnGrid/L{n}/Power``      — PV inverters on the grid side.

A path a site does not have reads as 0, so summing all three is correct for
DC-coupled, AC-coupled and mixed sites alike — no per-site profile choice
needed. The legacy ``dc_coupled`` / ``ac_coupled`` names stay as aliases for
sites that already pinned them; the only difference is that ``dc_coupled``
also reads the ``pvinverter`` service directly, which ``auto`` gets through
``PvOnGrid``/``PvOnOutput`` anyway.
"""

from __future__ import annotations

from . import base


def _pv_power(reader: base.DbusReader) -> float:
    dc = reader.get_float(base.SVC_SYSTEM, "/Dc/Pv/Power")
    on_output = sum(base.phases_float(reader, base.SVC_SYSTEM, "/Ac/PvOnOutput/L{n}/Power"))
    on_grid = sum(base.phases_float(reader, base.SVC_SYSTEM, "/Ac/PvOnGrid/L{n}/Power"))
    total = dc + on_output + on_grid
    return total if total > 0 else 0.0


def build_energy_data(reader: base.DbusReader, grid_export_limit_w: float):
    return base.build_energy_data(reader, grid_export_limit_w)


def build_metrics_data(reader: base.DbusReader, grid_export_limit_w: float):
    return base.build_metrics_data(reader, grid_export_limit_w, _pv_power)
