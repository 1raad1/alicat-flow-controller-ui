"""The rig's vocabulary: burner roles, zones, and the gases that fill them.

These names are the contract between the assignment screen, the ignition
sequence and the CSV columns.
"""

from __future__ import annotations

#: Roles in display order, paired with the label an operator sees.
ROLES = (
    ('nh3_rich', 'NH3 Stage 1'),
    ('h2_rich', 'H2 Stage 1'),
    ('ch4_stage1', 'CH4 Stage 1'),
    ('nh3_lean', 'NH3 Stage 2'),
    ('h2_lean', 'H2 Stage 2'),
    ('ch4_stage2', 'CH4 Stage 2'),
    ('nh3_pilot', 'NH3 Pilot'),
    ('h2_pilot', 'H2 Pilot'),
    ('ch4_pilot', 'CH4 Pilot'),
    ('rich_air', 'Air Stage 1'),
    ('lean_air', 'Air Stage 2'),
)

ROLE_LABELS = dict(ROLES)

#: Stage grouping used by the live cards, so a reading sits next to the other
#: readings it has to be judged against rather than next to its own gas.
STAGES = (
    ('Stage 1', ('nh3_rich', 'h2_rich', 'ch4_stage1', 'rich_air')),
    ('Stage 2', ('nh3_lean', 'h2_lean', 'ch4_stage2', 'lean_air')),
    ('Pilot', ('nh3_pilot', 'h2_pilot', 'ch4_pilot')),
)

FUEL_KEYS = frozenset({
    'nh3_rich', 'h2_rich', 'ch4_stage1',
    'nh3_lean', 'h2_lean', 'ch4_stage2',
    'nh3_pilot', 'h2_pilot', 'ch4_pilot',
})
AIR_KEYS = frozenset({'rich_air', 'lean_air'})

#: Roles whose setpoint is always approached as a ramp rather than a step.
#: The pilot and the air stages feed a lit flame; a step change there is a
#: pressure transient into the burner, not just a different number.
RAMP_KEYS = frozenset({
    'nh3_pilot', 'h2_pilot', 'ch4_pilot', 'rich_air', 'lean_air',
})

BASE_GAS_TYPES = ('Air', 'NH3', 'H2', 'CH4')
UNASSIGNED_ZONE = '-- unassigned --'
UNSELECTED_GAS = '-- select --'
ZONE_OPTIONS = (UNASSIGNED_ZONE, 'Zone 1', 'Zone 2', 'Pilot', 'General')

#: (gas, zone) -> role.  A pair absent from this map is a valid assignment
#: that simply has no part in the RQL calculations; it is still monitored and
#: still logged.
ROLE_MAP = {
    ('NH3', 'Zone 1'): 'nh3_rich',
    ('H2', 'Zone 1'): 'h2_rich',
    ('CH4', 'Zone 1'): 'ch4_stage1',
    ('Air', 'Zone 1'): 'rich_air',
    ('NH3', 'Zone 2'): 'nh3_lean',
    ('H2', 'Zone 2'): 'h2_lean',
    ('CH4', 'Zone 2'): 'ch4_stage2',
    ('Air', 'Zone 2'): 'lean_air',
    ('NH3', 'Pilot'): 'nh3_pilot',
    ('H2', 'Pilot'): 'h2_pilot',
    ('CH4', 'Pilot'): 'ch4_pilot',
}

#: Lower heating values, MJ/kg, and densities at standard conditions, kg/m^3.
#: Every supported pilot gas is already represented here, so pilot flow is
#: included in the live power estimate by the same calculation as stage fuel.
LHV_NH3 = 18.6
LHV_H2 = 120.0
LHV_CH4 = 50.0
RHO_NH3 = 0.7069
RHO_H2 = 0.0827
RHO_CH4 = 0.6558

#: Gas colours are part of the vocabulary rather than the theme: an operator
#: learns that hydrogen is blue, and it must not move when the theme does.
#: The theme may override individual entries.
GAS_COLORS = {
    'NH3': '#f25d38',
    'H2': '#4ea8de',
    'CH4': '#f0b429',
    'Air': '#7f8c8d',
}


def role_for(gas, zone):
    """The RQL role a (gas, zone) pair fills, or ``None``."""
    return ROLE_MAP.get((gas, zone))


def build_assignments(selection):
    """Map roles to units from ``{unit: (gas, zone)}``.

    Returns ``(assignments, custom)`` where ``assignments`` covers the RQL
    roles and ``custom`` holds every other selected unit keyed by its unit
    letter, so nothing an operator selected is quietly dropped from
    monitoring or logging.
    """
    assignments = {key: None for key, _label in ROLES}
    custom = {}
    for unit, (gas, zone) in selection.items():
        if gas in (UNSELECTED_GAS, '', None) or zone == UNASSIGNED_ZONE:
            continue
        key = role_for(gas, zone)
        if key is not None and assignments.get(key) is None:
            assignments[key] = unit
        else:
            custom[unit] = f"custom_{unit}"
    return assignments, custom
