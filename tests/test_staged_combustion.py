"""Stage-aware combustion aggregation across supported fuel gases."""

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from flow_controller.core.combustion_prefs import SCOPE_STAGE1, SCOPE_STAGE2
from flow_controller.core.session import FlowSession


class StagedCombustionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = FlowSession(
            worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(self.session.shutdown)
        self.session.selection = {
            'A': ('CH4', 'Zone 1'),
            'B': ('CH4', 'Zone 2'),
            'C': ('CH4', 'Pilot'),
            'D': ('Air', 'Zone 1'),
            'E': ('Air', 'Zone 2'),
        }
        self.session._rebuild_assignments()
        self.samples = {
            'A': {'flow': 2.0},
            'B': {'flow': 3.0},
            'C': {'flow': 1.0},
            'D': {'flow': 20.0},
            'E': {'flow': 30.0},
        }

    def test_stage_one_combines_its_methane_with_the_pilot(self):
        fuels, air, inert = self.session.combustion_flows(
            SCOPE_STAGE1, self.samples)

        self.assertEqual(fuels['CH4'], 3.0)
        self.assertEqual(air, 20.0)
        self.assertEqual(inert, 0.0)

    def test_stage_two_includes_its_own_methane(self):
        fuels, air, inert = self.session.combustion_flows(
            SCOPE_STAGE2, self.samples)

        self.assertEqual(fuels['CH4'], 3.0)
        self.assertEqual(air, 30.0)
        self.assertEqual(inert, 0.0)

    def test_phi_values_include_each_methane_line_once(self):
        stage1, stage2, global_phi = self.session.phi_values(self.samples)

        self.assertAlmostEqual(
            stage1, self.session.calc.phi(0.0, 0.0, 20.0, 3.0))
        self.assertAlmostEqual(
            stage2, self.session.calc.phi(0.0, 0.0, 30.0, 3.0))
        self.assertAlmostEqual(
            global_phi, self.session.calc.phi(0.0, 0.0, 50.0, 6.0))

    def test_each_supported_pilot_fuel_enters_stage_one_and_global_balance(self):
        expected = {
            'NH3': (1.0, 0.0, 2.0),
            'H2': (0.0, 1.0, 2.0),
            'CH4': (0.0, 0.0, 3.0),
        }
        for gas, (nh3, h2, ch4) in expected.items():
            with self.subTest(gas=gas):
                self.session.selection['C'] = (gas, 'Pilot')
                self.session._rebuild_assignments()

                fuels, air, inert = self.session.combustion_flows(
                    SCOPE_STAGE1, self.samples)
                self.assertEqual(fuels['NH3'], nh3)
                self.assertEqual(fuels['H2'], h2)
                self.assertEqual(fuels['CH4'], ch4)
                self.assertEqual(air, 20.0)
                self.assertEqual(inert, 0.0)

                stage1, stage2, global_phi = self.session.phi_values(
                    self.samples)
                self.assertAlmostEqual(
                    stage1, self.session.calc.phi(nh3, h2, 20.0, ch4))
                self.assertAlmostEqual(
                    stage2, self.session.calc.phi(0.0, 0.0, 30.0, 3.0))
                self.assertAlmostEqual(
                    global_phi,
                    self.session.calc.phi(nh3, h2, 50.0, ch4 + 3.0))


if __name__ == '__main__':
    unittest.main()
