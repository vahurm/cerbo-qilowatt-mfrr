"""Telemetry profiles — how PV power is summed for the METRICS block.

The default `auto` profile sums every PV source on com.victronenergy.system
and fits any topology; `dc_coupled` / `ac_coupled` are the legacy per-topology
names and remain accepted. The profile is chosen via `QW_TELEMETRY_PROFILE`
(see .env.example). Each profile exposes `build_energy_data(reader, limit)` and
`build_metrics_data(reader, limit)`.
"""

from __future__ import annotations

from . import ac_coupled, auto, dc_coupled
from .base import DbusReader

DEFAULT_PROFILE = "auto"

_PROFILES = {
    "auto": auto,
    "dc_coupled": dc_coupled,
    "ac_coupled": ac_coupled,
}


def get_profile(name: str):
    """Return the telemetry profile module for `name` (default: auto)."""
    key = (name or DEFAULT_PROFILE).strip().lower()
    if key not in _PROFILES:
        raise ValueError(
            "Unknown telemetry profile %r (known: %s)"
            % (name, ", ".join(sorted(_PROFILES)))
        )
    return _PROFILES[key]


__all__ = ["DbusReader", "get_profile"]
