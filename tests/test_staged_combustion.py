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

    def test_general_assignments_do_not_enter_staged_phi(self):
        expected = self.session.phi_values(self.samples)
        self.session.selection.update({
            'F': ('NH3', 'General'),
            'G': ('Air', 'General'),
            'H': ('N2', 'Zone 1'),
        })
        self.session._rebuild_assignments()
        self.samples.update({unit: {'flow': 100.0} for unit in 'FGH'})

        self.assertEqual(self.session.phi_values(self.samples), expected)
        fuels, air, inert = self.session.combustion_flows(samples=self.samples)
        self.assertEqual(fuels['NH3'], 100.0)
        self.assertEqual(air, 150.0)
        self.assertEqual(inert, 100.0)

    def test_mixed_stage_fuels_and_pilot_match_explicit_balance(self):
        self.session.selection.update({
            'F': ('NH3', 'Zone 1'), 'G': ('H2', 'Zone 1'),
            'H': ('NH3', 'Zone 2'), 'I': ('H2', 'Zone 2'),
        })
        self.samples.update({unit: {'flow': value} for unit, value in
                             zip('FGHI', (4.0, 5.0, 6.0, 7.0))})
        for gas in ('NH3', 'H2', 'CH4'):
            with self.subTest(pilot=gas):
                self.session.selection['C'] = (gas, 'Pilot')
                self.session._rebuild_assignments()
                pilot_nh3, pilot_h2, pilot_ch4 = (float(gas == fuel)
                                                for fuel in ('NH3', 'H2', 'CH4'))
                expected = (
                    self.session.calc.phi(4 + pilot_nh3, 5 + pilot_h2, 20, 2 + pilot_ch4),
                    self.session.calc.phi(6, 7, 30, 3),
                    self.session.calc.phi(10 + pilot_nh3, 12 + pilot_h2, 50, 5 + pilot_ch4),
                )
                for actual, target in zip(self.session.phi_values(self.samples), expected):
                    self.assertAlmostEqual(actual, target)


if __name__ == '__main__':
    unittest.main()
