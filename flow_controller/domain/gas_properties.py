"""Shared reference basis and gas properties for flow calculations.

Controller flows use Alicat's default standard conditions and the matching
Gas Select 5.0 densities. The dry-air composition used for combustion is kept
separate from the 20.9 percent convention used by emissions correction.
"""

from __future__ import annotations


# Alicat default standard conditions for SLPM.
STANDARD_TEMPERATURE_C = 25.0
STANDARD_PRESSURE_PSIA = 14.696

# Standard dry-air mole fractions. The residual contains argon, carbon
# dioxide, and other trace gases; O2 and N2 are deliberately not normalized.
AIR_O2_FRACTION = 0.209390
AIR_N2_FRACTION = 0.780848
AIR_OTHER_FRACTION = 1.0 - AIR_O2_FRACTION - AIR_N2_FRACTION

# Emissions reporting convention, distinct from physical dry-air composition.
O2_CORRECTION_AIR_PERCENT = 20.9

LHV_MJ_PER_KG = {
    "NH3": 18.6,
    "H2": 120.0,
    "CH4": 50.0,
}

# Alicat Gas Select 5.0 densities at 25 deg C and 14.696 psia, kg/m^3.
DENSITY_KG_PER_M3 = {
    "NH3": 0.70352,
    "H2": 0.08235,
    "CH4": 0.65688,
    "Air": 1.18402,
}

# Derived at one consistent standard-volume basis.
LHV_MJ_PER_M3 = {
    fuel: lhv * DENSITY_KG_PER_M3[fuel]
    for fuel, lhv in LHV_MJ_PER_KG.items()
}
