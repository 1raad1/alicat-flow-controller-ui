"""The combustion arithmetic the live estimate is drawn from.

Two things are being pinned down here.  The first is the physics: the oxygen
balance, the heating values and the geometry, checked against figures worked
out by hand rather than against whatever the code happened to return the first
time.  The second is what the module does when the rig is *not* burning --
which is the state the screen actually spends most of its time in.  A meter
that did not answer, an inlet whose bore nobody declared, a fuel line open with
the air still shut: none of those is an error, and none of them may be
answered with a number that looks like a measurement.
"""

import math
import unittest

from flow_controller.domain import combustion
from flow_controller.domain.combustion import CombustionCalculator
from flow_controller.domain.gas_properties import (
    AIR_N2_FRACTION, AIR_O2_FRACTION, AIR_OTHER_FRACTION,
    DENSITY_KG_PER_M3, LHV_MJ_PER_KG, LHV_MJ_PER_M3,
    O2_CORRECTION_AIR_PERCENT, STANDARD_PRESSURE_PSIA,
    STANDARD_TEMPERATURE_C,
)
from flow_controller.domain.rql import AutoCalcRequest, auto_calc


class GasPropertyTests(unittest.TestCase):
    def test_alicat_default_stp_and_densities_are_exact(self):
        self.assertEqual(STANDARD_TEMPERATURE_C, 25.0)
        self.assertEqual(STANDARD_PRESSURE_PSIA, 14.696)
        self.assertEqual(DENSITY_KG_PER_M3, {
            "NH3": 0.70352, "H2": 0.08235,
            "CH4": 0.65688, "Air": 1.18402})
        self.assertEqual(combustion.DENSITY, DENSITY_KG_PER_M3)

    def test_combustion_air_and_emissions_conventions_remain_distinct(self):
        self.assertEqual(AIR_O2_FRACTION, 0.209390)
        self.assertEqual(AIR_N2_FRACTION, 0.780848)
        self.assertAlmostEqual(AIR_OTHER_FRACTION, 0.009762, places=15)
        self.assertEqual(combustion.O2_IN_AIR, AIR_O2_FRACTION)
        self.assertEqual(combustion.N2_IN_AIR, AIR_N2_FRACTION)
        self.assertEqual(O2_CORRECTION_AIR_PERCENT, 20.9)

    def test_volumetric_lhv_is_derived_at_the_same_reference_basis(self):
        self.assertEqual(LHV_MJ_PER_KG, {"NH3": 18.6, "H2": 120.0, "CH4": 50.0})
        for fuel in combustion.FUELS:
            self.assertEqual(
                LHV_MJ_PER_M3[fuel],
                LHV_MJ_PER_KG[fuel] * DENSITY_KG_PER_M3[fuel])

    def test_requested_and_reconstructed_power_are_inverse_for_blends(self):
        requested_kw = 10.0
        for h2_fraction in (0.1, 0.3, 0.7, 0.9):
            with self.subTest(h2_fraction=h2_fraction):
                targets = auto_calc(AutoCalcRequest(
                    power_kw=requested_kw, h2_fraction=h2_fraction,
                    phi_stage1=1.1, phi_global=0.8, split_rich=0.7))
                measured_kw = combustion.power_kw({
                    "NH3": targets["nh3_rich"] + targets["nh3_lean"],
                    "H2": targets["h2_rich"] + targets["h2_lean"],
                })
                self.assertAlmostEqual(measured_kw, requested_kw, places=12)


class CombustionCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.calculator = CombustionCalculator()

    def test_stoichiometric_air_includes_all_fuels(self):
        expected = ((0.75 * 2.0 + 0.5 * 3.0 + 2.0 * 1.0)
                    / AIR_O2_FRACTION)
        self.assertAlmostEqual(
            self.calculator.stoich_air(2.0, 3.0, 1.0), expected)

    def test_phi_is_zero_without_positive_air(self):
        self.assertEqual(self.calculator.phi(1.0, 1.0, 0.0), 0.0)
        self.assertEqual(self.calculator.phi(1.0, 1.0, -1.0), 0.0)

    def test_phi_is_stoichiometric_at_calculated_air(self):
        air = self.calculator.stoich_air(4.0, 2.0, 0.5)
        self.assertAlmostEqual(self.calculator.phi(4.0, 2.0, air, 0.5), 1.0)


class FlowCleaningTests(unittest.TestCase):
    """A controller that said nothing contributes nothing."""

    def test_missing_and_unreadable_flows_read_as_zero(self):
        for value in (None, '', 'n/a', float('nan'), float('inf')):
            self.assertEqual(combustion.stoich_air_for({'CH4': value}), 0.0,
                             repr(value))

    def test_a_negative_flow_reads_as_zero(self):
        # Alicat meters report a small negative flow on a closed line at zero.
        # Subtracting that from the fuel demand would make a lean mixture look
        # leaner than it is.
        self.assertEqual(combustion.stoich_air_for({'H2': -0.004}), 0.0)

    def test_numeric_strings_are_accepted(self):
        self.assertAlmostEqual(combustion.stoich_air_for({'H2': '2'}),
                               0.5 * 2.0 / AIR_O2_FRACTION)

    def test_normalise_covers_every_fuel(self):
        self.assertEqual(set(combustion.normalise({'H2': 1.0})),
                         set(combustion.FUELS))


class PowerTests(unittest.TestCase):
    def test_power_per_slpm_matches_the_heating_values(self):
        # (MJ/kg * kg/m^3) / 60 = kW per L/min.
        for fuel in combustion.FUELS:
            expected = (combustion.LHV_MJ_PER_KG[fuel]
                        * combustion.DENSITY[fuel] / 60.0)
            self.assertAlmostEqual(combustion.KW_PER_SLPM[fuel], expected)

    def test_methane_is_about_half_a_kilowatt_per_slpm(self):
        # A figure worth pinning: it is the one an operator can sanity-check
        # against the pilot without doing any arithmetic.
        self.assertEqual(combustion.KW_PER_SLPM['CH4'], 0.5474)

    def test_power_adds_the_fuels_up(self):
        power = combustion.power_kw({'CH4': 1.0, 'H2': 2.0, 'NH3': 3.0})
        expected = (combustion.KW_PER_SLPM['CH4']
                    + 2.0 * combustion.KW_PER_SLPM['H2']
                    + 3.0 * combustion.KW_PER_SLPM['NH3'])
        self.assertAlmostEqual(power, expected)

    def test_air_is_not_a_fuel(self):
        self.assertEqual(combustion.power_kw({'Air': 100.0}), 0.0)


class BulkVelocityTests(unittest.TestCase):
    def test_velocity_is_flow_over_area(self):
        # 60 SLPM through 10 mm: 1e-3 m^3/s over pi/4 * 1e-4 m^2.
        expected = 1e-3 / (math.pi * 0.01 ** 2 / 4.0)
        self.assertAlmostEqual(combustion.bulk_velocity(60.0, 10.0), expected)

    def test_no_declared_bore_means_no_answer(self):
        for diameter in (None, '', 0, -5, 'wide'):
            self.assertIsNone(combustion.bulk_velocity(60.0, diameter),
                              repr(diameter))

    def test_a_shut_rig_has_a_velocity_of_zero_not_no_answer(self):
        # The distinction the tile depends on: the bore is known, so the
        # answer is a real zero rather than a blank.
        self.assertEqual(combustion.bulk_velocity(0.0, 25.0), 0.0)


class BlendTests(unittest.TestCase):
    def test_fractions_are_by_volume_and_sum_to_one(self):
        blend = combustion.blend_fractions({'NH3': 7.0, 'H2': 3.0})
        self.assertAlmostEqual(blend['NH3'], 0.7)
        self.assertAlmostEqual(blend['H2'], 0.3)
        self.assertAlmostEqual(sum(blend.values()), 1.0)

    def test_a_dry_rig_has_no_blend_rather_than_a_third_each(self):
        self.assertEqual(combustion.blend_fractions({}), {})
        self.assertEqual(combustion.blend_fractions({'H2': 0.0}), {})


class EstimateTests(unittest.TestCase):
    def test_everything_agrees_at_stoichiometric_methane(self):
        air = combustion.stoich_air_for({'CH4': 1.0})
        estimate = combustion.estimate({'CH4': 1.0}, air, diameter_mm=25.0)
        self.assertAlmostEqual(estimate.phi, 1.0)
        self.assertAlmostEqual(estimate.stoich_air, air)
        self.assertAlmostEqual(estimate.afr_volume, air)
        self.assertAlmostEqual(estimate.afr_stoich_volume, air)
        self.assertAlmostEqual(estimate.afr_volume, 9.5516, places=4)
        self.assertAlmostEqual(estimate.afr_stoich_mass, 17.22, places=2)
        self.assertEqual(estimate.power_kw, 0.5474)
        self.assertTrue(estimate.burning)

    def test_phi_matches_the_calculator_the_csv_uses(self):
        estimate = combustion.estimate({'NH3': 4.0, 'H2': 2.0, 'CH4': 0.5},
                                       30.0)
        self.assertAlmostEqual(
            estimate.phi, CombustionCalculator().phi(4.0, 2.0, 30.0, 0.5))

    def test_fuel_with_no_air_is_not_an_infinitely_rich_mixture(self):
        estimate = combustion.estimate({'H2': 5.0}, 0.0)
        self.assertEqual(estimate.phi, 0.0)
        self.assertFalse(estimate.burning)
        # No air per unit of fuel, which is a real ratio rather than a
        # missing one -- the fuel is measured, the air is measured, and the
        # answer is zero.
        self.assertEqual(estimate.afr_volume, 0.0)
        # The stoichiometric ratio is a property of the fuel, not of what is
        # being supplied, so it stands even with the air shut.
        self.assertAlmostEqual(estimate.afr_stoich_mass, 34.33, places=2)
        # The power is still real: the hydrogen is flowing whether or not
        # there is air to burn it in, which is a thing worth being able to see.
        self.assertGreater(estimate.power_kw, 0.0)

    def test_air_with_no_fuel_has_no_ratios(self):
        estimate = combustion.estimate({}, 50.0)
        self.assertEqual(estimate.phi, 0.0)
        self.assertEqual(estimate.power_kw, 0.0)
        self.assertIsNone(estimate.afr_volume)
        self.assertIsNone(estimate.afr_stoich_volume)
        self.assertIsNone(estimate.afr_stoich_mass)
        self.assertEqual(estimate.blend, {})

    def test_a_non_reacting_flow_moves_the_velocity_and_nothing_else(self):
        fuels, air = {'CH4': 1.0}, 20.0
        plain = combustion.estimate(fuels, air, diameter_mm=25.0)
        diluted = combustion.estimate(fuels, air, diameter_mm=25.0, inert=5.0)
        self.assertAlmostEqual(diluted.phi, plain.phi)
        self.assertAlmostEqual(diluted.power_kw, plain.power_kw)
        self.assertAlmostEqual(diluted.total_flow, plain.total_flow + 5.0)
        self.assertGreater(diluted.velocity, plain.velocity)

    def test_the_empty_estimate_is_safe_to_display(self):
        self.assertEqual(combustion.EMPTY.phi, 0.0)
        self.assertIsNone(combustion.EMPTY.velocity)
        self.assertFalse(combustion.EMPTY.burning)


if __name__ == "__main__":
    unittest.main()
