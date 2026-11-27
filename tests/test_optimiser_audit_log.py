"""Saved optimiser windows only cite a flow log that covered the whole window."""

import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import test_qt_optimiser as qt_base


class OptimiserAuditLogTests(unittest.TestCase):
    setUpClass = classmethod(qt_base.QtOptimiserTests.setUpClass.__func__)
    setUp = qt_base.QtOptimiserTests.setUp
    cleanup_session = qt_base.QtOptimiserTests.cleanup_session
    publish = qt_base.QtOptimiserTests.publish

    def start_log(self, name):
        self.assertTrue(self.session.start_logging(Path(self.directory.name) / name))
        self.addCleanup(self.session.stop_logging)
        return str(self.session.log_path)

    def record(self):
        changes, messages = [], []
        self.controller.changed.connect(lambda: changes.append(True))
        self.controller.message.connect(messages.append)
        return changes, messages

    def run_window(self, during=None):
        self.controller.start_window(True, True)
        self.publish(2)
        if during is not None:
            during()
        self.publish(5)
        window = self.controller.finish_window()
        self.assertIsNone(self.controller.capture)
        self.assertEqual(self.controller.experiment.pending["window"], window)
        return window

    def test_window_cites_log_that_stays_active(self):
        path = self.start_log("flows.csv")
        window = self.run_window()
        self.assertEqual(window["flow_audit_log"], path)

    def test_same_log_reported_active_keeps_reference(self):
        path = self.start_log("flows.csv")
        window = self.run_window(
            lambda: self.session.logging_changed.emit(True, Path(path)))
        self.assertEqual(window["flow_audit_log"], path)

    def test_log_stopped_mid_window_drops_reference_and_window_saves(self):
        path = self.start_log("flows.csv")
        changes, messages = self.record()

        def stop():
            self.session.stop_logging()
            self.assertIsNone(self.controller.capture.flow_audit_log)
            self.assertTrue(changes)
            self.assertIn(path, messages[-1])
            self.assertIn(path, self.controller.last_message)

        window = self.run_window(stop)
        self.assertNotIn("flow_audit_log", window)

    def test_log_switched_to_other_path_mid_window_drops_reference(self):
        self.start_log("first.csv")

        def switch():
            self.session.stop_logging()
            self.start_log("second.csv")

        window = self.run_window(switch)
        self.assertNotIn("flow_audit_log", window)

    def test_active_signal_for_other_path_drops_reference(self):
        self.start_log("first.csv")
        other = Path(self.directory.name) / "second.csv"
        window = self.run_window(
            lambda: self.session.logging_changed.emit(True, other))
        self.assertNotIn("flow_audit_log", window)

    def test_logging_signal_without_capture_does_nothing(self):
        path = self.start_log("flows.csv")
        self.assertIsNone(self.controller.capture)
        changes, messages = self.record()
        before = self.controller.last_message
        self.session.logging_changed.emit(False, path)
        self.session.logging_changed.emit(True, Path(self.directory.name) / "other.csv")
        self.assertEqual(changes, [])
        self.assertEqual(messages, [])
        self.assertEqual(self.controller.last_message, before)
        self.assertIsNone(self.controller.capture)


if __name__ == "__main__":
    unittest.main()
