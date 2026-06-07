"""Tests for the per-topology PV-power profiles and profile selection."""

from __future__ import annotations

import pytest

from telemetry import ac_coupled, base, dc_coupled, get_profile

SYS = base.SVC_SYSTEM
PVINV = "com.victronenergy.pvinverter.cg_30"


def _reader(values=None, services=None):
    from conftest import FakeReader

    return FakeReader(values=values or {}, services=services or {})


# --------------------------------------------------------------------------- #
# DC-coupled (site A): /Dc/Pv/Power + optional pvinverter /Ac/Power
# --------------------------------------------------------------------------- #

def test_dc_only_mppt_when_no_pvinverter():
    reader = _reader(values={(SYS, "/Dc/Pv/Power"): 4000})
    assert dc_coupled._pv_power(reader) == 4000.0


def test_dc_plus_ac_coupled_pvinverter():
    reader = _reader(
        values={(SYS, "/Dc/Pv/Power"): 4000, (PVINV, "/Ac/Power"): 500},
        services={base.SVC_PVINVERTER_PREFIX: PVINV},
    )
    assert dc_coupled._pv_power(reader) == 4500.0


def test_dc_clamps_negative_night_self_consumption_to_zero():
    reader = _reader(
        values={(SYS, "/Dc/Pv/Power"): 0, (PVINV, "/Ac/Power"): -30},
        services={base.SVC_PVINVERTER_PREFIX: PVINV},
    )
    assert dc_coupled._pv_power(reader) == 0.0


def test_dc_negative_mppt_clamped_to_zero():
    reader = _reader(values={(SYS, "/Dc/Pv/Power"): -5})
    assert dc_coupled._pv_power(reader) == 0.0


# --------------------------------------------------------------------------- #
# AC-coupled (site B): sum of /Ac/PvOnOutput/L{n}/Power
# --------------------------------------------------------------------------- #

def test_ac_coupled_sums_three_phases():
    reader = _reader(
        values={
            (SYS, "/Ac/PvOnOutput/L1/Power"): 1000,
            (SYS, "/Ac/PvOnOutput/L2/Power"): 1100,
            (SYS, "/Ac/PvOnOutput/L3/Power"): 900,
        }
    )
    assert ac_coupled._pv_power(reader) == 3000.0


# --------------------------------------------------------------------------- #
# Profile selection
# --------------------------------------------------------------------------- #

def test_get_profile_known():
    assert get_profile("dc_coupled") is dc_coupled
    assert get_profile("ac_coupled") is ac_coupled


def test_get_profile_defaults_to_dc_coupled():
    assert get_profile("") is dc_coupled
    assert get_profile(None) is dc_coupled


def test_get_profile_is_case_and_space_insensitive():
    assert get_profile("  DC_Coupled  ") is dc_coupled


def test_get_profile_unknown_raises():
    with pytest.raises(ValueError):
        get_profile("bogus")
