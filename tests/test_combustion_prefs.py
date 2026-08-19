"""The combustion estimate's own settings: inlet bores, and how fast it runs.

The file behind this module is read at start-up and written whenever the
operator changes one of the figures, so like the per-unit store it has to
survive a missing directory, a half-written file and a hand edit.  What is
worth checking beyond that is the meaning of an *absent* diameter: it is not
zero and not a default, it is "nobody has said", and it is the only thing
standing between the velocity tile and a number computed against a guess.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flow_controller.core import combustion_prefs


class CleanDiameterTests(unittest.TestCase):
    def test_a_number_or_a_numeric_string_is_a_declaration(self):
        self.assertEqual(combustion_prefs.clean_diameter(25), 25.0)
        self.assertEqual(combustion_prefs.clean_diameter('12.5'), 12.5)

    def test_nothing_typed_is_no_declaration(self):
        for value in (None, '', '  ', 'wide', object()):
            self.assertIsNone(combustion_prefs.clean_diameter(value),
                              repr(value))

    def test_zero_and_negatives_withdraw_the_declaration(self):
        # Rather than clamping to something tiny: a bore of nothing would give
        # an infinite velocity, and "not declared" is the safe reading.
        for value in (0, 0.0, -1, -25.0):
            self.assertIsNone(combustion_prefs.clean_diameter(value),
                              repr(value))

    def test_a_bore_past_the_ceiling_is_held_at_it(self):
        self.assertEqual(combustion_prefs.clean_diameter(1e6),
                         combustion_prefs.MAX_DIAMETER_MM)

    def test_infinities_and_nan_are_not_declarations(self):
        for value in (float('inf'), float('-inf'), float('nan')):
            self.assertIsNone(combustion_prefs.clean_diameter(value),
                              repr(value))


class CleanIntervalTests(unittest.TestCase):
    def test_one_pass_is_the_floor(self):
        for value in (1, 0, -5, 0.4):
            self.assertEqual(combustion_prefs.clean_interval(value), 1,
                             repr(value))

    def test_a_whole_number_of_passes_comes_back(self):
        self.assertEqual(combustion_prefs.clean_interval('10'), 10)
        self.assertEqual(combustion_prefs.clean_interval(7.9), 7)

    def test_garbage_falls_back_to_every_pass(self):
        self.assertEqual(combustion_prefs.clean_interval('often'), 1)

    def test_the_interval_has_a_ceiling(self):
        self.assertEqual(combustion_prefs.clean_interval(10 ** 9),
                         combustion_prefs.MAX_INTERVAL)


class CleanInletCountTests(unittest.TestCase):
    def test_count_is_a_whole_number_with_a_floor_of_one(self):
        for value in (None, '', 'many', 0, -4):
            self.assertEqual(combustion_prefs.clean_inlet_count(value), 1)
        self.assertEqual(combustion_prefs.clean_inlet_count('4'), 4)
        self.assertEqual(combustion_prefs.clean_inlet_count(3.9), 3)

    def test_count_has_a_typo_ceiling(self):
        self.assertEqual(combustion_prefs.clean_inlet_count(10 ** 6),
                         combustion_prefs.MAX_INLET_COUNT)


class CleanLiveTests(unittest.TestCase):
    def test_an_absent_setting_means_the_estimate_runs(self):
        self.assertTrue(combustion_prefs.clean_live(None))
        self.assertTrue(combustion_prefs.clean(None)['live'])

    def test_hand_edited_words_are_read(self):
        for value in ('true', 'YES', 'on', '1'):
            self.assertTrue(combustion_prefs.clean_live(value), value)
        for value in ('false', 'no', 'off', '0', ''):
            self.assertFalse(combustion_prefs.clean_live(value), value)


class CleanTests(unittest.TestCase):
    def test_a_file_of_nonsense_still_yields_working_settings(self):
        for raw in (None, [], 'nope', 42):
            self.assertEqual(combustion_prefs.clean(raw),
                             dict(combustion_prefs.DEFAULTS), repr(raw))

    def test_one_bad_field_does_not_take_the_others_with_it(self):
        prefs = combustion_prefs.clean(
            {'inlet_mm': 'wide', 'stage1_mm': 25, 'stage2_inlets': 4,
             'interval': 5})
        self.assertIsNone(prefs['inlet_mm'])
        self.assertEqual(prefs['stage1_mm'], 25.0)
        self.assertEqual(prefs['stage2_inlets'], 4)
        self.assertEqual(prefs['interval'], 5)

    def test_unknown_keys_are_dropped(self):
        prefs = combustion_prefs.clean({'inlet_mm': 30, 'colour': 'blue'})
        self.assertNotIn('colour', prefs)
        self.assertEqual(prefs['inlet_mm'], 30.0)


class LoadAndSaveTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / 'combustion_prefs.json'
        self.addCleanup(self._dir.cleanup)

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(combustion_prefs.load(self.path),
                         dict(combustion_prefs.DEFAULTS))

    def test_a_half_written_file_is_not_an_error(self):
        self.path.write_text('{"inlet_mm": 25,', encoding='utf-8')
        self.assertEqual(combustion_prefs.load(self.path),
                         dict(combustion_prefs.DEFAULTS))

    def test_settings_survive_a_round_trip(self):
        prefs = dict(combustion_prefs.DEFAULTS,
                     inlet_mm=30.0, stage1_mm=25.0, stage2_inlets=6,
                     live=False, interval=10)
        self.assertIsNone(combustion_prefs.save(prefs, self.path))
        self.assertEqual(combustion_prefs.load(self.path), prefs)

    def test_an_undeclared_bore_is_left_out_of_the_file(self):
        combustion_prefs.save(dict(combustion_prefs.DEFAULTS, stage1_mm=25.0),
                              self.path)
        written = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(written['stage1_mm'], 25.0)
        self.assertNotIn('inlet_mm', written)
        self.assertNotIn('stage2_mm', written)

    def test_a_directory_that_does_not_exist_yet_is_made(self):
        nested = Path(self._dir.name) / 'deeper' / 'combustion_prefs.json'
        self.assertIsNone(combustion_prefs.save({'inlet_mm': 25.0}, nested))
        self.assertTrue(nested.exists())

    def test_an_unwritable_path_is_reported_rather_than_raised(self):
        # The setting is in force for this session either way; failing to
        # write it must not interrupt whatever the operator was doing.
        error = combustion_prefs.save({'inlet_mm': 25.0},
                                      Path(self._dir.name))
        self.assertIsInstance(error, str)

    def test_the_environment_variable_moves_the_file(self):
        with patch.dict(os.environ,
                        {combustion_prefs.ENV_VAR: str(self.path)}):
            self.assertEqual(combustion_prefs.path(), self.path)


if __name__ == "__main__":
    unittest.main()
