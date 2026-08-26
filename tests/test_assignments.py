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
        self.assertEqual(problems, ["Missing for Rich + quench-air: CH4/Pilot"])

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


if __name__ == "__main__":
    unittest.main()
