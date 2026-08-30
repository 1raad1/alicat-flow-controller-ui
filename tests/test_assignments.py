import unittest

from flow_controller.domain.assignments import (
    FULL_RQL,
    RICH_QUENCH,
    assess_autocalc,
)
from flow_controller.domain import roles


class AssignmentTests(unittest.TestCase):
    def test_full_rql_configuration(self):
        mode, problems = assess_autocalc(FULL_RQL)
        self.assertEqual(mode, "FULL_RQL")
        self.assertEqual(problems, [])

    def test_rich_quench_configuration(self):
        mode, problems = assess_autocalc(RICH_QUENCH)
        self.assertEqual(mode, "RICH_QUENCH")
        self.assertEqual(problems, [])

    def test_duplicate_required_assignment_is_reported(self):
        pairs = list(RICH_QUENCH) + [("Air", "Zone 2")]
        mode, problems = assess_autocalc(pairs)
        self.assertEqual(mode, "RICH_QUENCH")
        self.assertEqual(
            problems,
            ["Air to Zone 2 is set on 2 units (must be 1)"],
        )

    def test_missing_assignments_report_nearest_configuration(self):
        pairs = RICH_QUENCH - {("CH4", "Pilot")}
        mode, problems = assess_autocalc(pairs)
        self.assertIsNone(mode)
        self.assertEqual(
            problems,
            ["Missing for Rich + quench-air: "
             "one of NH3/Pilot, H2/Pilot, CH4/Pilot"],
        )

    def test_each_supported_fuel_can_fill_the_pilot_role(self):
        core = RICH_QUENCH - {("CH4", "Pilot")}
        for gas, role in (("NH3", "nh3_pilot"),
                          ("H2", "h2_pilot"),
                          ("CH4", "ch4_pilot")):
            with self.subTest(gas=gas):
                mode, problems = assess_autocalc(core | {(gas, "Pilot")})
                self.assertEqual(mode, "RICH_QUENCH")
                self.assertEqual(problems, [])

                assignments, custom = roles.build_assignments({
                    'P': (gas, 'Pilot'),
                })
                self.assertEqual(assignments[role], 'P')
                self.assertEqual(custom, {})

    def test_more_than_one_pilot_fuel_is_rejected(self):
        pairs = RICH_QUENCH | {("NH3", "Pilot")}
        mode, problems = assess_autocalc(pairs)
        self.assertIsNone(mode)
        self.assertEqual(
            problems,
            ["Pilot is assigned to CH4, NH3 (select exactly one fuel)"],
        )

    def test_methane_can_be_assigned_to_both_stages_and_the_pilot(self):
        assignments, custom = roles.build_assignments({
            'A': ('CH4', 'Zone 1'),
            'B': ('CH4', 'Zone 2'),
            'C': ('CH4', 'Pilot'),
        })

        self.assertEqual(assignments['ch4_stage1'], 'A')
        self.assertEqual(assignments['ch4_stage2'], 'B')
        self.assertEqual(assignments['ch4_pilot'], 'C')
        self.assertEqual(custom, {})

    def test_all_pilot_roles_are_ramp_protected_fuels(self):
        for role in ('nh3_pilot', 'h2_pilot', 'ch4_pilot'):
            with self.subTest(role=role):
                self.assertIn(role, roles.FUEL_KEYS)
                self.assertIn(role, roles.RAMP_KEYS)


if __name__ == "__main__":
    unittest.main()
