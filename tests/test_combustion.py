import unittest

from flow_controller.domain.combustion import CombustionCalculator


class CombustionCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.calculator = CombustionCalculator()

    def test_stoichiometric_air_includes_all_fuels(self):
        expected = (0.75 * 2.0 + 0.5 * 3.0 + 2.0 * 1.0) / 0.21
        self.assertAlmostEqual(
            self.calculator.stoich_air(2.0, 3.0, 1.0), expected)

    def test_phi_is_zero_without_positive_air(self):
        self.assertEqual(self.calculator.phi(1.0, 1.0, 0.0), 0.0)
        self.assertEqual(self.calculator.phi(1.0, 1.0, -1.0), 0.0)

    def test_phi_is_stoichiometric_at_calculated_air(self):
        air = self.calculator.stoich_air(4.0, 2.0, 0.5)
        self.assertAlmostEqual(self.calculator.phi(4.0, 2.0, air, 0.5), 1.0)


if __name__ == "__main__":
    unittest.main()

