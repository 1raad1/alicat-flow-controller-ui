"""Combustion arithmetic on measured flows, independent of the interface.

Everything here answers the same question from a different angle: given the
volumetric flows the meters are reporting right now, what is the burner
actually doing?  Four numbers come out of it --

* **phi**, the equivalence ratio, from the oxygen the fuel demands against the
  oxygen the air carries;
* **thermal power**, from the lower heating value of each fuel;
* **the stoichiometric air requirement** and the air/fuel ratios beside it,
  which is what phi is measured against;
* **bulk velocity** at the inlet, which needs a diameter the software cannot
  know and the operator has to declare.

All flows are volumetric and referenced to the same standard conditions the
mass flow controllers are calibrated in, so a ratio of two of them is a ratio
of two mole counts and no density enters into it.  Densities only appear where
a *mass* is genuinely wanted -- the heating values are per kilogram, and the
mass air/fuel ratio is what a combustion text tabulates -- and both are derived
from one molar volume rather than from the per-gas densities in
:mod:`~flow_controller.domain.roles`, which were taken at slightly different
temperatures and would make the two routes disagree in the third decimal.

Nothing here reads a widget or a controller.  The session hands it numbers and
gets numbers back, which is what lets a test check the physics without a rig.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .roles import LHV_CH4, LHV_H2, LHV_NH3

#: The fuels the rig burns, in the order they are displayed.
FUELS = ('CH4', 'H2', 'NH3')

#: Moles of O2 demanded per mole of fuel:
#:   CH4 + 2 O2    -> CO2 + 2 H2O    (2.00 mol O2 / mol CH4)
#:   2 H2 + O2     -> 2 H2O          (0.50 mol O2 / mol H2)
#:   4 NH3 + 3 O2  -> 2 N2 + 6 H2O   (0.75 mol O2 / mol NH3)
#: Oxygen balance for the combined CH4/H2/NH3 + air reaction
#: (Note 8 Feb 2023):  0.21 * a = 2*CH4 + 0.5*H2 + 0.75*NH3
O2_PER_FUEL = {'CH4': 2.00, 'H2': 0.50, 'NH3': 0.75}

#: Mole fraction of O2 in dry air.
O2_IN_AIR = 0.21

#: g/mol.  Air is the usual dry-air average.
MOLAR_MASS = {'CH4': 16.043, 'H2': 2.016, 'NH3': 17.031, 'Air': 28.965}

#: Lower heating values, MJ/kg, keyed the way the flows are.
LHV_MJ_PER_KG = {'CH4': LHV_CH4, 'H2': LHV_H2, 'NH3': LHV_NH3}

#: Litres per mole at the reference conditions behind the "S" in SLPM.  Alicat
#: standard litres are referenced to 25 C and one atmosphere, so that is the
#: molar volume every conversion below uses.
STD_MOLAR_VOLUME_L = 24.465

#: Thermal power per SLPM of each fuel, kW.  Precomputed because it is the
#: inner term of a calculation that runs on every acquisition pass:
#:   MJ/kg * g/mol = kJ/mol;  / (L/mol) = kJ/L;  / 60 s = kW per L/min.
KW_PER_SLPM = {fuel: LHV_MJ_PER_KG[fuel] * MOLAR_MASS[fuel]
               / STD_MOLAR_VOLUME_L / 60.0
               for fuel in FUELS}

#: Density of each gas at the same reference conditions, kg/m^3 -- which is
#: g/L, which is why this is a plain division.
DENSITY = {gas: mass / STD_MOLAR_VOLUME_L for gas, mass in MOLAR_MASS.items()}

_INFINITIES = (float('inf'), float('-inf'))


def _clean(value):
    """A flow as a finite non-negative float.  Anything else reads as zero.

    Samples arrive as whatever the telemetry parsed, which is ``None`` for a
    controller that did not answer.  A line that said nothing contributes
    nothing to a sum; it does not stop a display refresh halfway through with
    a ``TypeError``.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in _INFINITIES:
        return 0.0
    return number if number > 0.0 else 0.0


def normalise(fuels):
    """``{fuel: SLPM}`` covering every known fuel, cleaned.

    Callers pass whichever fuels they happen to have -- a pilot-only stage has
    one, the standard rig has three -- and get back all of them, so the code
    downstream can index without asking whether a key is there.
    """
    fuels = fuels or {}
    return {fuel: _clean(fuels.get(fuel)) for fuel in FUELS}


def stoich_air_for(fuels):
    """SLPM of air that would burn ``fuels`` exactly, with nothing left over."""
    oxygen = sum(O2_PER_FUEL[fuel] * flow
                 for fuel, flow in normalise(fuels).items())
    return oxygen / O2_IN_AIR


def phi_for(fuels, air):
    """Equivalence ratio: the stoichiometric air need over the air supplied.

    Zero means *no answer* rather than a very lean flame -- there is either no
    fuel to burn or no air to burn it in, and in both cases a ratio would be
    describing a mixture that is not there.  The views print ``--`` for it.
    """
    air = _clean(air)
    if air <= 0.0:
        return 0.0
    need = stoich_air_for(fuels)
    return need / air if need > 0.0 else 0.0


def power_kw(fuels):
    """Thermal power of the fuel flowing, kW, on lower heating values.

    This is heat *released* by complete combustion, not shaft or electrical
    output: it is the firing rate the rig is running at, and it is the same
    quantity the auto-calculation takes as its input, so a stored target of
    10 kW should read back as about 10 kW once the flows have settled.
    """
    return sum(KW_PER_SLPM[fuel] * flow
               for fuel, flow in normalise(fuels).items())


def bulk_velocity(total_slpm, diameter_mm):
    """Bulk velocity through a round inlet, m/s, or ``None`` without a bore.

    Volumetric flow over cross-sectional area, both at standard conditions:
    this is the cold-flow velocity the mixture enters at, not a velocity in the
    flame, and it knows nothing about preheat.  ``None`` when no diameter has
    been declared, because a velocity computed against a guessed bore is a
    number that looks like a measurement and is not one.
    """
    try:
        diameter = float(diameter_mm)
    except (TypeError, ValueError):
        return None
    if not diameter > 0.0:
        return None
    area = math.pi * (diameter / 1000.0) ** 2 / 4.0        # m^2
    return _clean(total_slpm) / 60000.0 / area             # (m^3/s) / m^2


def mass_flow_kg_s(flows):
    """``{gas: SLPM}`` as a mass flow, kg/s, for any gas in :data:`DENSITY`."""
    return sum(DENSITY[gas] * _clean(flow) / 60000.0
               for gas, flow in (flows or {}).items() if gas in DENSITY)


def blend_fractions(fuels):
    """Each fuel's share of the fuel volume, 0..1, or an empty map if dry.

    Volume fractions rather than mass, because that is what the meters read and
    what an operator sets a blend in -- "70/30 NH3/H2" on this rig has always
    meant by volume, and the auto-calculation takes its H2 percentage the same
    way.
    """
    fuels = normalise(fuels)
    total = sum(fuels.values())
    if total <= 0.0:
        return {}
    return {fuel: flow / total for fuel, flow in fuels.items()}


@dataclass(frozen=True)
class CombustionEstimate:
    """What one set of measured flows says the burner is doing.

    A value object rather than a tuple: this crosses from the session to the
    view whole, and a view that unpacked ten positional numbers would be one
    inserted field away from labelling every tile wrongly.
    """

    fuels: dict = field(default_factory=dict)     # {fuel: SLPM}
    fuel_total: float = 0.0                       # SLPM
    air: float = 0.0                              # SLPM
    stoich_air: float = 0.0                       # SLPM
    phi: float = 0.0
    power_kw: float = 0.0
    velocity: float | None = None                 # m/s at the declared inlet
    afr_volume: float | None = None               # air/fuel as supplied, vol
    afr_stoich_volume: float | None = None        # air/fuel at phi = 1, vol
    afr_stoich_mass: float | None = None          # air/fuel at phi = 1, mass
    blend: dict = field(default_factory=dict)     # {fuel: volume fraction}
    inert: float = 0.0                            # SLPM of everything else

    @property
    def total_flow(self):
        """Everything entering the inlet, SLPM -- diluent and purge included.

        Separate from ``fuel_total + air`` because a nitrogen line does not
        burn but does occupy the duct, and a velocity that ignored it would
        be low by exactly the flow the operator added.
        """
        return self.fuel_total + self.air + self.inert

    @property
    def burning(self):
        """True when there is both fuel and air -- i.e. phi means something."""
        return self.fuel_total > 0.0 and self.air > 0.0


#: An estimate of nothing, for a paused calculation or an idle rig.  Frozen and
#: shared, so pausing does not allocate on every pass it skips.
EMPTY = CombustionEstimate()


def estimate(fuels, air, diameter_mm=None, inert=0.0):
    """Every derived number for one set of flows, in one pass over them.

    Grouped into a single call because the tiles are read together and must
    therefore agree with each other: computing phi from this pass and the power
    from the next would put two moments of the rig side by side on one card.
    """
    fuels = normalise(fuels)
    air = _clean(air)
    inert = _clean(inert)
    fuel_total = sum(fuels.values())
    need = stoich_air_for(fuels)

    fuel_mass = mass_flow_kg_s(fuels)
    return CombustionEstimate(
        fuels=fuels,
        fuel_total=fuel_total,
        air=air,
        stoich_air=need,
        phi=(need / air if air > 0.0 and need > 0.0 else 0.0),
        power_kw=power_kw(fuels),
        velocity=bulk_velocity(fuel_total + air + inert, diameter_mm),
        afr_volume=(air / fuel_total if fuel_total > 0.0 else None),
        afr_stoich_volume=(need / fuel_total if fuel_total > 0.0 else None),
        afr_stoich_mass=(mass_flow_kg_s({'Air': need}) / fuel_mass
                         if fuel_mass > 0.0 else None),
        blend=blend_fractions(fuels),
        inert=inert,
    )


class CombustionCalculator:
    """The rig's combustion arithmetic, as one object a session can hold.

    The methods are thin wrappers over the functions above.  They stay because
    the RQL auto-calculation takes a calculator to work through -- which is how
    a test substitutes one -- and because the older ``(nh3, h2, ch4)`` argument
    order is what the ignition path and the CSV writer already call.
    """

    # Kept as class attributes: they were public, and a rig script reading
    # ``calc.O2_PER_CH4`` should keep working.
    O2_PER_CH4 = O2_PER_FUEL['CH4']
    O2_PER_NH3 = O2_PER_FUEL['NH3']
    O2_PER_H2 = O2_PER_FUEL['H2']
    O2_IN_AIR = O2_IN_AIR

    def stoich_air(self, nh3_flow, h2_flow, ch4_flow=0.0):
        return stoich_air_for(
            {'NH3': nh3_flow, 'H2': h2_flow, 'CH4': ch4_flow})

    def phi(self, nh3_flow, h2_flow, air_flow, ch4_flow=0.0):
        return phi_for({'NH3': nh3_flow, 'H2': h2_flow, 'CH4': ch4_flow},
                       air_flow)

    def power_kw(self, fuels):
        return power_kw(fuels)

    def bulk_velocity(self, total_slpm, diameter_mm):
        return bulk_velocity(total_slpm, diameter_mm)

    def estimate(self, fuels, air, diameter_mm=None, inert=0.0):
        return estimate(fuels, air, diameter_mm, inert)
