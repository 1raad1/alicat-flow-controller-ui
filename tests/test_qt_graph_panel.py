import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from flow_controller.core.graph_history import GraphHistory as CoreGraphHistory
from flow_controller.ui.qt_graph_panel import GraphHistory, QtGraphPanel


class CoreHistoryView:
    def __init__(self, history):
        self.history = history

    @property
    def times(self):
        return self.history.times()

    @property
    def revision(self):
        return self.history.revision

    def values(self, unit, metric):
        return self.history.raw(unit, metric)

    def unit_meta(self, unit):
        return {"label": f"Unit {unit}", "color": "#ffffff"}


class QtGraphPanelRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_revision_skips_duplicate_frame_but_detects_bounded_rotation(self):
        history = CoreGraphHistory(limit=2)
        history.set_units([1])
        started = datetime(2026, 1, 1)
        history.push(1, started, {1: {"flow": 1}})
        history.push(2, started + timedelta(seconds=1), {1: {"flow": 2}})
        panel = QtGraphPanel(CoreHistoryView(history))
        self.addCleanup(panel.close)
        panel.set_selection([(1, "flow")])
        curve = panel._curves[(1, "flow")]
        curve.setData = Mock(wraps=curve.setData)

        panel.render_frame()
        panel.render_frame()
        self.assertEqual(curve.setData.call_count, 1)

        history.push(3, started + timedelta(seconds=2), {1: {"flow": 3}})
        panel.render_frame()
        self.assertEqual(curve.setData.call_count, 2)
        self.assertEqual(curve.getData()[1].tolist(), [2.0, 3.0])

    def test_selection_rebuild_invalidates_last_rendered_revision(self):
        history = CoreGraphHistory(limit=2)
        history.set_units([1])
        history.push(1, datetime(2026, 1, 1), {1: {"flow": 1}})
        panel = QtGraphPanel(CoreHistoryView(history))
        self.addCleanup(panel.close)
        panel.set_selection([(1, "flow")])
        panel.render_frame()

        panel.set_selection([(1, "flow")])
        curve = panel._curves[(1, "flow")]
        curve.setData = Mock(wraps=curve.setData)
        panel.render_frame()
        self.assertEqual(curve.setData.call_count, 1)

    def test_synthetic_history_without_revision_keeps_rendering(self):
        history = GraphHistory()
        history.times = [0.0]
        history.set_unit_meta(1, label="Unit 1", color="#ffffff")
        history.set_series(1, "flow", [1.0])
        panel = QtGraphPanel(history)
        self.addCleanup(panel.close)
        panel.set_selection([(1, "flow")])
        curve = panel._curves[(1, "flow")]
        curve.setData = Mock(wraps=curve.setData)

        panel.render_frame()
        panel.render_frame()
        self.assertEqual(curve.setData.call_count, 2)

    def test_sparse_spike_gets_an_axis_check_within_five_timer_ticks(self):
        history = CoreGraphHistory(limit=10)
        history.set_units([1])
        started = datetime(2026, 1, 1)
        history.push(1, started, {1: {"flow": 1}})
        panel = QtGraphPanel(CoreHistoryView(history))
        self.addCleanup(panel.close)
        panel.set_selection([(1, "flow")])
        panel.render_frame()
        initial_high = panel._plots["flow"].getViewBox().viewRange()[1][1]
        self.assertLess(initial_high, 100)
        panel._update_limits = Mock(wraps=panel._update_limits)

        history.push(2, started + timedelta(seconds=5), {1: {"flow": 100}})
        for _ in range(panel.LIMIT_CHECK_FRAMES):
            panel.render_frame()

        updated_high = panel._plots["flow"].getViewBox().viewRange()[1][1]
        self.assertGreater(updated_high, 100)
        self.assertFalse(panel._limits_pending)
        self.assertEqual(panel._update_limits.call_count, 1)
        for _ in range(panel.LIMIT_CHECK_FRAMES * 2):
            panel.render_frame()
        self.assertEqual(panel._update_limits.call_count, 1)

    def test_clear_and_smaller_limit_refresh_existing_curves(self):
        history = CoreGraphHistory(limit=3)
        history.set_units([1])
        started = datetime(2026, 1, 1)
        for generation in range(1, 4):
            history.push(
                generation,
                started + timedelta(seconds=generation - 1),
                {1: {"flow": generation}},
            )
        panel = QtGraphPanel(CoreHistoryView(history))
        self.addCleanup(panel.close)
        panel.set_selection([(1, "flow")])
        panel.render_frame()
        curve = panel._curves[(1, "flow")]

        history.set_limit(2)
        panel.render_frame()
        self.assertEqual(curve.getData()[1].tolist(), [2.0, 3.0])

        history.clear(generation=3)
        panel.render_frame()
        self.assertIsNone(curve.getData()[0])
        self.assertIsNone(curve.getData()[1])


if __name__ == "__main__":
    unittest.main()
