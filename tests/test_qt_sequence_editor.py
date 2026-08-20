"""Focused checks for precise sequence key-point editing."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from flow_controller.core.sequence import (HOLD, LINEAR, SMOOTH, Keyframe,
                                           Sequence, Track)
from flow_controller.ui.qt_sequence_panel import CurveEditor, KeyPointDialog


class SequenceEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.track = Track(
            key='nh3_rich', label='NH3 rich',
            keyframes=[Keyframe(0.0, 1.0, HOLD),
                       Keyframe(5.0, 4.0, LINEAR),
                       Keyframe(10.0, 2.0, HOLD)])

    def test_editor_constrains_time_between_neighbouring_points(self):
        dialog = KeyPointDialog(self.track, index=1, when=5.0, value=4.0,
                                interp=LINEAR)
        self.assertAlmostEqual(dialog.time_spin.minimum(), 0.001)
        self.assertAlmostEqual(dialog.time_spin.maximum(), 9.999)
        self.assertEqual(dialog.interp_combo.currentData(), LINEAR)
        dialog.close()

    def test_opening_point_time_is_pinned_but_value_is_editable(self):
        dialog = KeyPointDialog(self.track, index=0, when=0.0, value=1.0)
        self.assertFalse(dialog.time_spin.isEnabled())
        self.assertTrue(dialog.value_spin.isEnabled())
        dialog.close()

    def test_delete_command_preserves_opening_point(self):
        editor = CurveEditor()
        editor.set_sequence(Sequence(tracks=[self.track]))
        editor.set_active(self.track.key)

        editor._delete_point(0)
        self.assertEqual(len(self.track.keyframes), 3)
        editor._delete_point(1)
        self.assertEqual([(frame.t, frame.value)
                          for frame in self.track.sorted_frames()],
                         [(0.0, 1.0), (10.0, 2.0)])
        editor.close()

    def test_context_transition_command_updates_selected_point(self):
        editor = CurveEditor()
        editor.set_sequence(Sequence(tracks=[self.track]))
        editor.set_active(self.track.key)

        editor._set_point_interp(0, LINEAR)
        self.assertEqual(self.track.sorted_frames()[0].interp, LINEAR)
        self.assertEqual(editor._selected_index, 0)
        editor.close()

    def test_smoothing_a_group_only_changes_transitions_inside_it(self):
        editor = CurveEditor()
        editor.set_sequence(Sequence(tracks=[self.track]))
        editor.set_active(self.track.key)
        editor.select_indices([0, 1])

        self.assertTrue(editor.smooth_selected())

        self.assertEqual([frame.interp
                          for frame in self.track.sorted_frames()],
                         [SMOOTH, LINEAR, HOLD])
        self.assertEqual(editor.selected_indices, (0, 1))
        editor.close()

    def test_a_disjoint_selection_only_smooths_adjacent_selected_points(self):
        editor = CurveEditor()
        editor.set_sequence(Sequence(tracks=[self.track]))
        editor.set_active(self.track.key)
        editor.select_indices([0, 2])

        self.assertFalse(editor.smooth_selected())
        self.assertEqual(self.track.sorted_frames()[0].interp, HOLD)
        editor.close()

    def test_overview_gives_air_a_secondary_y_axis(self):
        air = Track(
            key='rich_air', label='Stage 1 air', gas='Air',
            keyframes=[Keyframe(0.0, 20.0, HOLD),
                       Keyframe(10.0, 100.0, LINEAR)])
        editor = CurveEditor()
        editor.set_sequence(Sequence(tracks=[self.track, air]))

        self.assertTrue(editor.getPlotItem().getAxis('right').isVisible())
        self.assertEqual(editor._air_curve_keys, {'rich_air'})
        fuel_top = editor.getPlotItem().getViewBox().viewRange()[1][1]
        air_top = editor._air_view.viewRange()[1][1]
        self.assertLess(fuel_top, air_top)

        # Picking one track returns to the original dedicated editing scale.
        editor.set_active('rich_air')
        self.assertFalse(editor.getPlotItem().getAxis('right').isVisible())
        self.assertEqual(editor._air_curve_keys, set())
        self.assertGreater(
            editor.getPlotItem().getViewBox().viewRange()[1][1], 100.0)
        editor.close()

    def test_hiding_every_air_track_removes_the_secondary_axis(self):
        air = Track(
            key='custom_A', label='Air', gas='air',
            keyframes=[Keyframe(0.0, 10.0, HOLD),
                       Keyframe(10.0, 80.0, HOLD)])
        editor = CurveEditor()
        editor.set_sequence(Sequence(tracks=[self.track, air]))

        self.assertTrue(editor.getPlotItem().getAxis('right').isVisible())
        editor.set_hidden({'custom_A'})

        self.assertFalse(editor.getPlotItem().getAxis('right').isVisible())
        editor.close()


if __name__ == '__main__':
    unittest.main()
