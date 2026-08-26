import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from flow_controller.core.experiment_plan import (
    AbortProcedure, ExperimentPlan, PlanStage, RUN_IDLE, RUN_RUNNING,
)
from flow_controller.ui.qt_experiment_plan import ExperimentPlanPane
from flow_controller.ui.qt_widgets import Card


class FakeController(QObject):
    plan_changed = Signal(object)
    state_changed = Signal(str)
    stage_changed = Signal(str, int, int)
    attention_required = Signal(str)

    def __init__(self):
        super().__init__()
        self.plan = None
        self.plan_path = None
        self.state = RUN_IDLE
        self.started = 0
        self.aborted = 0
        self.reason = ""

    def start(self):
        self.started += 1

    def abort(self):
        self.aborted += 1

    def resolve_timeout(self, _decision):
        return True


class ExperimentPlanPaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_buttons_follow_loaded_and_running_state(self):
        controller = FakeController()
        pane = ExperimentPlanPane(controller)
        self.assertNotIsInstance(pane, Card)
        self.assertEqual(pane.load_button.text(), "Load test sequence…")
        self.assertFalse(pane.start_button.isEnabled())
        self.assertFalse(pane.abort_button.isEnabled())
        self.assertFalse(pane.resolve_button.isEnabled())

        controller.plan = ExperimentPlan(
            "test", AbortProcedure("zero_all"),
            (PlanStage("one", {"air": 1}),))
        controller.plan_changed.emit(controller.plan)
        self.assertTrue(pane.start_button.isEnabled())
        self.assertIn("abort: zero_all", pane.summary.text())

        controller.state = RUN_RUNNING
        controller.state_changed.emit(RUN_RUNNING)
        self.assertFalse(pane.load_button.isEnabled())
        self.assertTrue(pane.abort_button.isEnabled())

    def test_stage_signal_is_visible(self):
        controller = FakeController()
        pane = ExperimentPlanPane(controller)
        controller.stage_changed.emit("settle", 2, 4)
        self.assertEqual(pane.stage.text(), "Stage 2 of 4: settle")


if __name__ == "__main__":
    unittest.main()
