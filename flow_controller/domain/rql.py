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


#: Roles whose flows enter through the Stage 1 inlet while the pilot is off.
STAGE1_INLET_ROLES = ('nh3_rich', 'h2_rich', 'rich_air')


def stage1_bulk_velocity(flows, area_mm2):
    """Cold-flow bulk velocity at the Stage 1 inlet, m/s, from role flows.

    Stage 1 fuel plus Stage 1 air, in SLPM, over the declared inlet area.
    Both are at standard conditions, the same convention as the live velocity
    tile: this is not a hot-gas velocity and knows nothing about preheat.
    """
    total = sum(float(flows.get(role, 0.0) or 0.0) for role in STAGE1_INLET_ROLES)
    return total / 60000.0 / (float(area_mm2) * 1e-6)       # (m^3/s) / m^2


def power_for_stage1_velocity(velocity_m_s, area_mm2, h2_fraction, phi_stage1,
                              split_rich=1.0, calculator=None):
    """Thermal input, kW, that gives ``velocity_m_s`` at the Stage 1 inlet.

    The Stage 1 inlet carries the Stage 1 fuel and the air that sets
    ``phi_stage1``.  Per litre of Stage 1 fuel that air is the stoichiometric
    requirement of the blend divided by ``phi_stage1``, so the fuel is the
    inlet flow over ``1 + air-per-fuel``.  Stage 1 fuel over ``split_rich`` is
    the total fuel, and the heating value turns that into a firing rate.  The
    result goes through :func:`auto_calc` unchanged, which makes bulk velocity
    and thermal input two descriptions of the same operating point rather than
    two calculations that could drift apart.
    """
    calc = calculator or CombustionCalculator()
    nh3_frac = 1.0 - h2_fraction
    stage1_total = velocity_m_s * area_mm2 * 1e-6 * 60000.0          # SLPM
    air_per_fuel = calc.stoich_air(nh3_frac, h2_fraction) / phi_stage1
    fuel_stage1 = stage1_total / (1.0 + air_per_fuel)                # SLPM
    fuel_total = fuel_stage1 / split_rich
    return calc.power_kw({'NH3': fuel_total * nh3_frac,
                          'H2': fuel_total * h2_fraction})


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
    # Use the same Alicat default-STP densities as measured-power
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
