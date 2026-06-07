"""Telemetry profiles — select the per-site topology (DC- vs AC-coupled PV).

The profile is chosen via the `QW_TELEMETRY_PROFILE` environment variable
(see .env.example). Each profile exposes `build_energy_data(reader, limit)` and
`build_metrics_data(reader, limit)`.
"""

from __future__ import annotations

from . import ac_coupled, dc_coupled
from .base import DbusReader

_PROFILES = {
    "dc_coupled": dc_coupled,
    "ac_coupled": ac_coupled,
}


def get_profile(name: str):
    """Return the telemetry profile module for `name` (default: dc_coupled)."""
    key = (name or "dc_coupled").strip().lower()
    if key not in _PROFILES:
        raise ValueError(
            "Unknown telemetry profile %r (known: %s)"
            % (name, ", ".join(sorted(_PROFILES)))
        )
    return _PROFILES[key]


__all__ = ["DbusReader", "get_profile"]
