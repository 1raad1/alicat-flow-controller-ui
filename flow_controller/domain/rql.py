"""Rich-quench-lean flow targets from a thermal power request.

Given a firing rate and the two equivalence ratios the operator wants, this
works out the volumetric flow each controller has to deliver.  It is pure
arithmetic on numbers -- no widgets, no hardware -- which is what lets the
same calculation be checked by a test and reused by whichever view is on
screen.

The fuel is an ammonia/hydrogen blend specified by *volume* fraction, because
that is what the rotameters and the mass flow controllers are calibrated in,
but the heating value is per *mass*.  The conversion between the two is the
only subtle step below.
"""

from __future__ import annotations

from dataclasses import dataclass

from .combustion import CombustionCalculator, DENSITY
from .roles import LHV_H2, LHV_NH3

#: Assignment configurations this calculation is defined for.
FULL_RQL = "FULL_RQL"
RICH_QUENCH = "RICH_QUENCH"


class AutoCalcError(ValueError):
    """A request that cannot be met, with an operator-readable explanation."""


@dataclass(frozen=True)
class AutoCalcRequest:
    """What the operator asked for."""

    power_kw: float
    h2_fraction: float        # volume fraction of the fuel blend, 0..1
    phi_stage1: float
    phi_global: float
    split_rich: float = 1.0   # fraction of total fuel through stage 1, 0..1

    def validate(self, config=FULL_RQL):
        if config == FULL_RQL and not 0.0 < self.split_rich <= 1.0:
            raise AutoCalcError("Fuel split must be between 0 and 100 %.")
        if not 0.0 < self.h2_fraction < 1.0:
            raise AutoCalcError("H2 fraction must be between 0 and 100 %.")
        if self.phi_stage1 <= 0 or self.phi_global <= 0:
            raise AutoCalcError("φ values must be > 0.")
        if self.power_kw <= 0:
            raise AutoCalcError("Total power must be > 0 kW.")


def auto_calc(request, config=FULL_RQL, calculator=None):
    """Volumetric targets in SLPM, keyed by role.

    Raises :class:`AutoCalcError` when the requested pair of equivalence
    ratios is unreachable -- which happens when stage 1 is asked to be leaner
    than the global mixture, leaving a negative amount of air for stage 2.
    The message says which number to move, because "negative air" on its own
    tells an operator nothing about what to do next.
    """
    calc = calculator or CombustionCalculator()
    split_rich = request.split_rich if config == FULL_RQL else 1.0
    request.validate(config)

    h2_frac = request.h2_fraction
    nh3_frac = 1.0 - h2_frac

    # Volume fractions -> mass fractions, so the heating values apply.
    # Use the same 25 °C standard-volume convention as measured-power
    # reconstruction, so requested and observed power are inverse operations.
    rho_mix = nh3_frac * DENSITY['NH3'] + h2_frac * DENSITY['H2']
    m_nh3 = nh3_frac * DENSITY['NH3'] / rho_mix
    m_h2 = h2_frac * DENSITY['H2'] / rho_mix
    lhv_mix = m_nh3 * LHV_NH3 + m_h2 * LHV_H2

    m_dot = request.power_kw / (lhv_mix * 1000.0)      # kg/s
    v_total = m_dot / rho_mix * 60.0 * 1000.0          # SLPM
    v_rich = v_total * split_rich
    v_lean = v_total * (1.0 - split_rich)

    nh3_rich = v_rich * nh3_frac
    h2_rich = v_rich * h2_frac
    nh3_lean = v_lean * nh3_frac
    h2_lean = v_lean * h2_frac

    air_stoich_rich = calc.stoich_air(nh3_rich, h2_rich)
    air_stoich_total = calc.stoich_air(nh3_rich + nh3_lean, h2_rich + h2_lean)
    air_rich = air_stoich_rich / request.phi_stage1
    air_total = air_stoich_total / request.phi_global
    air_lean = air_total - air_rich

    if air_lean < 0:
        if config == FULL_RQL:
            min_phi = split_rich * request.phi_global
            raise AutoCalcError(
                "Lean air is negative.\n"
                f"φ_s1 must be ≥ {min_phi:.3f} "
                "(= split × φ_global).\n"
                f"You entered φ_s1 = {request.phi_stage1:.3f}.")
        raise AutoCalcError(
            "Quench air is negative.\n"
            f"φ_global ({request.phi_global}) cannot be richer than "
            f"φ_stage1 ({request.phi_stage1}).\n"
            "Lower φ_global or raise φ_stage1.")

    return {
        'nh3_rich': nh3_rich,
        'h2_rich': h2_rich,
        'nh3_lean': nh3_lean,
        'h2_lean': h2_lean,
        'rich_air': air_rich,
        'lean_air': air_lean,
    }
