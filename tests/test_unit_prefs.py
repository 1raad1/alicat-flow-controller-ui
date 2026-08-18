"""Declared per-controller settings: a small, forgiving JSON store by unit id.

The file that backs this module is meant to survive a missing directory, a
half-written file, or a stray edit by hand.  These tests lean on that: they
throw garbage at ``load`` and expect ``{}`` back, not an exception.

Two fields share the file -- a meter's full scale and its ramp rate -- so most
of what is worth checking is that they are held to their own ceilings and that
one of them going missing does not take the other with it.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flow_controller.core import unit_prefs


class CleanTests(unittest.TestCase):
    """Boundaries of turning one raw value into a declared figure or None."""

    def test_a_plain_float_and_int_come_back_as_floats(self):
        self.assertEqual(unit_prefs.clean(50.0), 50.0)
        self.assertEqual(unit_prefs.clean(50), 50.0)
        self.assertIsInstance(unit_prefs.clean(50), float)

    def test_a_numeric_string_is_accepted(self):
        self.assertEqual(unit_prefs.clean('50'), 50.0)
        self.assertEqual(unit_prefs.clean('2.5'), 2.5)

    def test_zero_and_negatives_clear_the_declaration(self):
        for value in (0, 0.0, -1, -0.001):
            self.assertIsNone(unit_prefs.clean(value), repr(value))

    def test_nan_returns_none(self):
        self.assertIsNone(unit_prefs.clean(float('nan')))

    def test_infinity_returns_none(self):
        self.assertIsNone(unit_prefs.clean(float('inf')))

    def test_none_and_nonsense_return_none(self):
        for value in (None, 'abc', object()):
            self.assertIsNone(unit_prefs.clean(value), repr(value))

    def test_a_value_above_the_ceiling_is_clamped_to_it(self):
        self.assertEqual(unit_prefs.clean(unit_prefs.MAX_FULL_SCALE + 1),
                         unit_prefs.MAX_FULL_SCALE)

    def test_the_ceiling_itself_is_accepted_unchanged(self):
        self.assertEqual(unit_prefs.clean(unit_prefs.MAX_FULL_SCALE),
                         unit_prefs.MAX_FULL_SCALE)

    def test_an_explicit_ceiling_is_the_one_applied(self):
        self.assertEqual(unit_prefs.clean(500.0, 10.0), 10.0)


class CleanFieldTests(unittest.TestCase):
    """Each field is held to its own ceiling, and only known fields exist."""

    def test_a_full_scale_is_clamped_to_the_full_scale_ceiling(self):
        self.assertEqual(
            unit_prefs.clean_field('full_scale',
                                   unit_prefs.MAX_FULL_SCALE * 2),
            unit_prefs.MAX_FULL_SCALE)

    def test_a_ramp_is_clamped_to_the_ramp_ceiling(self):
        self.assertEqual(
            unit_prefs.clean_field('ramp', unit_prefs.MAX_RAMP_RATE * 2),
            unit_prefs.MAX_RAMP_RATE)

    def test_a_ramp_below_its_ceiling_is_kept_as_typed(self):
        self.assertEqual(unit_prefs.clean_field('ramp', 2.5), 2.5)

    def test_a_field_nobody_declares_is_not_stored(self):
        self.assertIsNone(unit_prefs.clean_field('colour', 1.0))

    def test_the_two_ceilings_are_not_the_same_number(self):
        # Otherwise the per-field clamping above would pass by coincidence.
        self.assertNotEqual(unit_prefs.MAX_FULL_SCALE,
                            unit_prefs.MAX_RAMP_RATE)


class CleanRecordTests(unittest.TestCase):
    """One unit's record: only the fields that mean something survive."""

    def test_a_record_with_both_fields_keeps_both(self):
        self.assertEqual(
            unit_prefs.clean_record({'full_scale': 50, 'ramp': 2.5}),
            {'full_scale': 50.0, 'ramp': 2.5})

    def test_a_field_that_does_not_clean_is_dropped_without_the_other(self):
        self.assertEqual(
            unit_prefs.clean_record({'full_scale': 50, 'ramp': 0}),
            {'full_scale': 50.0})
        self.assertEqual(
            unit_prefs.clean_record({'full_scale': 'abc', 'ramp': 2.5}),
            {'ramp': 2.5})

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(
            unit_prefs.clean_record({'full_scale': 50, 'colour': 'red'}),
            {'full_scale': 50.0})

    def test_an_empty_or_useless_record_cleans_to_empty(self):
        for raw in ({}, {'ramp': -1}, {'nothing': 'here'}, None, 'abc'):
            self.assertEqual(unit_prefs.clean_record(raw), {}, repr(raw))

    def test_a_bare_number_is_read_as_the_legacy_full_scale_form(self):
        # The file used to hold nothing but full scales, so figures already
        # typed in on a rig have to survive the move to records.
        self.assertEqual(unit_prefs.clean_record(50), {'full_scale': 50.0})
        self.assertEqual(unit_prefs.clean_record('2.5'), {'full_scale': 2.5})


class LoadTests(unittest.TestCase):
    """Reading the store back, including when it is not worth reading."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = Path(self.tmpdir.name) / 'unit_prefs.json'

    def write(self, text):
        self.store.write_text(text, encoding='utf-8')

    def test_a_missing_file_loads_as_empty(self):
        self.assertEqual(unit_prefs.load(store_path=self.store), {})

    def test_garbage_that_is_not_json_loads_as_empty(self):
        self.write('not { valid json at all')
        self.assertEqual(unit_prefs.load(store_path=self.store), {})

    def test_valid_json_that_is_not_a_dict_loads_as_empty(self):
        self.write('[1, 2]')
        self.assertEqual(unit_prefs.load(store_path=self.store), {})
        self.write('"hello"')
        self.assertEqual(unit_prefs.load(store_path=self.store), {})

    def test_a_well_formed_file_loads_as_records_of_floats(self):
        self.write(json.dumps({'A': {'full_scale': 50, 'ramp': '2.5'},
                               'B': {'ramp': 1}}))
        prefs = unit_prefs.load(store_path=self.store)
        self.assertEqual(prefs, {'A': {'full_scale': 50.0, 'ramp': 2.5},
                                 'B': {'ramp': 1.0}})
        for record in prefs.values():
            for value in record.values():
                self.assertIsInstance(value, float)

    def test_units_whose_records_are_empty_are_dropped(self):
        self.write(json.dumps({'A': {'full_scale': 50}, 'B': {},
                               'C': {'ramp': 0}, 'D': None, 'E': 'abc'}))
        self.assertEqual(unit_prefs.load(store_path=self.store),
                         {'A': {'full_scale': 50.0}})

    def test_a_legacy_file_of_bare_full_scales_still_loads(self):
        self.write(json.dumps({'A': 50, 'B': 2.5}))
        self.assertEqual(unit_prefs.load(store_path=self.store),
                         {'A': {'full_scale': 50.0},
                          'B': {'full_scale': 2.5}})

    def test_keys_come_back_as_str(self):
        self.write(json.dumps({'3': {'full_scale': 10}}))
        prefs = unit_prefs.load(store_path=self.store)
        self.assertIn('3', prefs)
        self.assertIsInstance(list(prefs.keys())[0], str)

    def test_values_above_their_ceilings_come_back_clamped(self):
        self.write(json.dumps({'A': {'full_scale': unit_prefs.MAX_FULL_SCALE + 500,
                                     'ramp': unit_prefs.MAX_RAMP_RATE + 500}}))
        self.assertEqual(unit_prefs.load(store_path=self.store)['A'],
                         {'full_scale': unit_prefs.MAX_FULL_SCALE,
                          'ramp': unit_prefs.MAX_RAMP_RATE})


class SaveTests(unittest.TestCase):
    """Writing it out, and reporting rather than raising when that fails."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = Path(self.tmpdir.name) / 'unit_prefs.json'

    def test_a_successful_save_round_trips_through_load(self):
        prefs = {'A': {'full_scale': 50.0, 'ramp': 2.5},
                 'B': {'full_scale': 2.5}}
        self.assertIsNone(unit_prefs.save(prefs, store_path=self.store))
        self.assertEqual(unit_prefs.load(store_path=self.store), prefs)

    def test_values_are_rounded_to_four_decimal_places(self):
        unit_prefs.save({'A': {'full_scale': 12.3456789, 'ramp': 0.123456}},
                        store_path=self.store)
        self.assertEqual(unit_prefs.load(store_path=self.store)['A'],
                         {'full_scale': 12.3457, 'ramp': 0.1235})

    def test_entries_that_do_not_clean_are_omitted_from_the_file(self):
        unit_prefs.save({'A': {'full_scale': 50.0, 'ramp': 0.0},
                         'B': {'full_scale': -3.0}, 'C': {}, 'D': None},
                        store_path=self.store)
        raw = json.loads(self.store.read_text(encoding='utf-8'))
        self.assertEqual(raw, {'A': {'full_scale': 50.0}})

    def test_a_legacy_bare_value_is_written_out_as_a_record(self):
        unit_prefs.save({'A': 50.0}, store_path=self.store)
        raw = json.loads(self.store.read_text(encoding='utf-8'))
        self.assertEqual(raw, {'A': {'full_scale': 50.0}})

    def test_a_missing_parent_directory_is_created(self):
        nested = (Path(self.tmpdir.name) / 'nested' / 'deeper'
                  / 'unit_prefs.json')
        result = unit_prefs.save({'A': {'ramp': 1.0}}, store_path=nested)
        self.assertIsNone(result)
        self.assertTrue(nested.exists())

    def test_an_empty_dict_writes_an_empty_object(self):
        unit_prefs.save({}, store_path=self.store)
        self.assertEqual(self.store.read_text(encoding='utf-8').strip(), '{}')
        self.assertEqual(unit_prefs.load(store_path=self.store), {})

    def test_an_unwritable_target_returns_an_error_string_not_a_raise(self):
        blocker = Path(self.tmpdir.name) / 'blocker'
        blocker.write_text('not a directory', encoding='utf-8')
        target = blocker / 'unit_prefs.json'
        try:
            result = unit_prefs.save({'A': {'ramp': 1.0}}, store_path=target)
        except Exception as exc:  # pragma: no cover - the point is it doesn't
            self.fail(f'save() raised instead of reporting an error: {exc!r}')
        self.assertIsInstance(result, str)
        self.assertTrue(result)


class PathTests(unittest.TestCase):
    def test_the_environment_override_is_used_verbatim(self):
        override = str(Path(tempfile.gettempdir()) / 'somewhere' / 'prefs.json')
        with patch.dict(os.environ, {unit_prefs.ENV_VAR: override}):
            self.assertEqual(unit_prefs.path(), Path(override))

    def test_without_the_override_the_path_ends_in_the_filename(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(unit_prefs.ENV_VAR, None)
            self.assertEqual(unit_prefs.path().name, unit_prefs.FILENAME)


if __name__ == '__main__':
    unittest.main()
