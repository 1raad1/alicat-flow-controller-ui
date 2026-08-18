import unittest

from flow_controller.domain.graphing import (
    auto_bar_span,
    padded_limits,
    parse_axis_limits,
    should_rescale,
)


class GraphAxisTests(unittest.TestCase):
    def test_automatic_axis_has_no_manual_limits(self):
        self.assertIsNone(parse_axis_limits(True, "bad", "values", "X axis"))

    def test_manual_axis_parses_numbers(self):
        self.assertEqual(
            parse_axis_limits(False, "-2.5", "10", "Y axis"),
            (-2.5, 10.0),
        )

    def test_manual_axis_requires_increasing_limits(self):
        with self.assertRaisesRegex(ValueError, "minimum"):
            parse_axis_limits(False, "5", "5", "Y axis")

    def test_manual_axis_reports_non_numeric_values(self):
        with self.assertRaisesRegex(ValueError, "numeric"):
            parse_axis_limits(False, "left", "right", "X axis")

    def test_manual_axis_rejects_non_finite_values(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            parse_axis_limits(False, "nan", "10", "Y axis")


class PaddedLimitTests(unittest.TestCase):
    def test_span_is_padded_symmetrically(self):
        self.assertEqual(padded_limits(0.0, 10.0, pad=0.1), (-1.0, 11.0))

    def test_reversed_bounds_are_ordered(self):
        self.assertEqual(padded_limits(10.0, 0.0, pad=0.1), (-1.0, 11.0))

    def test_flat_series_still_gets_a_visible_span(self):
        low, high = padded_limits(5.0, 5.0)
        self.assertLess(low, 5.0)
        self.assertGreater(high, 5.0)

    def test_flat_zero_series_does_not_collapse(self):
        low, high = padded_limits(0.0, 0.0)
        self.assertLess(low, high)

    def test_non_finite_bounds_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            padded_limits(float('nan'), 1.0)


class AutoBarSpanTests(unittest.TestCase):
    def test_an_idle_meter_gets_the_floor(self):
        # The defect this floor exists for: a span of zero is filled completely
        # by the first nonzero reading, so a freshly connected rig showed every
        # meter pegged before it had done anything.
        self.assertEqual(auto_bar_span(0.0), 1.0)

    def test_a_peak_below_the_floor_does_not_shrink_the_span(self):
        self.assertEqual(auto_bar_span(0.4), 1.0)

    def test_the_span_stays_above_the_peak(self):
        for peak in (0.5, 1.0, 7.5, 10.0, 42.0, 137.0, 950.0):
            self.assertGreater(auto_bar_span(peak), peak, peak)

    def test_the_bar_is_not_full_at_its_own_peak(self):
        # A span equal to the peak reads 100% at every new peak, which is the
        # pegged bar one sample later.
        self.assertLess(10.0 / auto_bar_span(10.0), 0.95)

    def test_spans_land_on_readable_figures(self):
        self.assertEqual(auto_bar_span(8.0), 10.0)
        self.assertEqual(auto_bar_span(10.0), 12.0)
        self.assertEqual(auto_bar_span(50.0), 60.0)
        self.assertEqual(auto_bar_span(100.0), 120.0)

    def test_the_top_of_a_decade_rolls_into_the_next(self):
        self.assertEqual(auto_bar_span(9.5), 12.0)

    def test_a_negative_peak_is_treated_as_idle(self):
        self.assertEqual(auto_bar_span(-5.0), 1.0)

    def test_non_finite_and_unparseable_peaks_fall_back_to_the_floor(self):
        # Both reach here from the sample cache, where a controller that is not
        # answering leaves whatever it last wrote.
        self.assertEqual(auto_bar_span(float('nan')), 1.0)
        self.assertEqual(auto_bar_span(float('inf')), 1.0)
        self.assertEqual(auto_bar_span(None), 1.0)
        self.assertEqual(auto_bar_span('--'), 1.0)

    def test_the_floor_can_be_declared_away(self):
        self.assertEqual(auto_bar_span(0.0, floor=0.0), 0.0)

    def test_the_span_is_a_clean_number(self):
        # Shown to the operator as well as drawn, so 12.000000000000002 will
        # not do.
        self.assertEqual(repr(auto_bar_span(10.0)), '12.0')


class RescaleDecisionTests(unittest.TestCase):
    def test_missing_limits_always_rescale(self):
        self.assertTrue(should_rescale(None, 0.0, 1.0))

    def test_data_inside_limits_is_left_alone(self):
        self.assertFalse(should_rescale((0.0, 10.0), 2.0, 8.0))

    def test_data_above_the_axis_forces_a_rescale(self):
        self.assertTrue(should_rescale((0.0, 10.0), 2.0, 12.0))

    def test_data_below_the_axis_forces_a_rescale(self):
        self.assertTrue(should_rescale((0.0, 10.0), -1.0, 8.0))

    def test_badly_shrunk_data_reclaims_the_axis(self):
        # Occupying a tenth of the axis wastes most of the plot area.
        self.assertTrue(should_rescale((0.0, 10.0), 4.0, 5.0))

    def test_mildly_shrunk_data_does_not_trigger_a_repaint(self):
        self.assertFalse(should_rescale((0.0, 10.0), 1.0, 9.0))

    def test_degenerate_axis_is_rescaled(self):
        self.assertTrue(should_rescale((5.0, 5.0), 5.0, 5.0))

    def test_non_finite_data_is_ignored(self):
        self.assertFalse(
            should_rescale((0.0, 10.0), float('nan'), float('nan')))


if __name__ == "__main__":
    unittest.main()
