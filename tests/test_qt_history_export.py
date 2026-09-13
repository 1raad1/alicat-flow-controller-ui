"""History export UI state across file selection, rebuilds and shutdown."""

import os
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from flow_controller.core.session import FlowSession
from flow_controller.ui.qt_logging_tab import LoggingTab
from flow_controller.ui.qt_main_window import MainWindow


class DeferredExporter(QObject):
    active_changed = Signal(bool)
    completed = Signal(str, int)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.active = False
        self.jobs = []
        self.can_shutdown = True
        self.cancel_requested = False

    def start(self, snapshot, path):
        self.jobs.append((snapshot, path))
        self.active = True
        self.active_changed.emit(True)
        return True

    def shutdown(self):
        return self.can_shutdown

    def cancel(self):
        self.cancel_requested = True
        return self.active


class HistoryExportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(self.session.shutdown)
        self.exporter = DeferredExporter()
        self.session.history_export = self.exporter
        self.session.history.set_units(['A'])
        self.session.history.push(1, datetime(2026, 9, 7), {'A': {'flow': 2.0}})

    def test_cancelled_file_dialog_does_not_prepare_or_start_export(self):
        tab = LoggingTab(self.session)
        self.addCleanup(tab.close)
        with (patch('flow_controller.ui.qt_logging_tab.QFileDialog.getSaveFileName',
                    return_value=('', '')),
              patch.object(self.session.history, 'export_snapshot') as snapshot):
            tab._export()
        snapshot.assert_not_called()
        self.assertEqual(self.exporter.jobs, [])

    def test_export_snapshot_and_busy_state_survive_widget_rebuild(self):
        window = MainWindow(self.session)
        self.addCleanup(window.close)
        with patch('flow_controller.ui.qt_logging_tab.QFileDialog.getSaveFileName',
                   return_value=('history.csv', 'CSV (*.csv)')):
            window.logging_tab._export()
        snapshot, path = self.exporter.jobs[0]
        self.assertEqual(path, Path('history.csv'))
        self.assertFalse(window.logging_tab._export_button.isEnabled())
        self.session.history.clear()
        self.assertEqual(list(snapshot.iter_rows())[0][1], 2.0)

        window._rebuild()
        self.assertFalse(window.logging_tab._export_button.isEnabled())
        window.logging_tab._export()
        self.assertEqual(len(self.exporter.jobs), 1)
        messages = []
        window.logging_tab.status.connect(messages.append)
        self.exporter.active = False
        self.exporter.active_changed.emit(False)
        self.exporter.completed.emit('history.csv', 1)
        self.assertTrue(window.logging_tab._export_button.isEnabled())
        self.assertIn('Exported 1 samples to history.csv', messages)

    def test_clear_during_file_dialog_does_not_start_empty_export(self):
        tab = LoggingTab(self.session)
        self.addCleanup(tab.close)

        def select_file(*_args):
            self.session.history.clear()
            return 'history.csv', 'CSV (*.csv)'

        with patch('flow_controller.ui.qt_logging_tab.QFileDialog.getSaveFileName',
                   side_effect=select_file):
            tab._export()
        self.assertEqual(self.exporter.jobs, [])

    def test_cancel_control_keeps_export_disabled_until_worker_finishes(self):
        tab = LoggingTab(self.session)
        self.addCleanup(tab.close)
        with patch('flow_controller.ui.qt_logging_tab.QFileDialog.getSaveFileName',
                   return_value=('history', 'Excel workbook (*.xlsx)')):
            tab._export()
        self.assertEqual(self.exporter.jobs[0][1], Path('history.xlsx'))
        tab._cancel_export_button.click()
        self.assertTrue(self.exporter.cancel_requested)
        self.assertFalse(tab._export_button.isEnabled())
        self.assertFalse(tab._cancel_export_button.isEnabled())
        self.exporter.active = False
        self.exporter.active_changed.emit(False)
        self.exporter.cancelled.emit()
        self.assertTrue(tab._export_button.isEnabled())
        self.assertTrue(tab._cancel_export_button.isHidden())

    def test_close_defers_session_shutdown_until_export_worker_stops(self):
        window = MainWindow(self.session)
        self.addCleanup(window.close)
        self.exporter.can_shutdown = False
        event = QCloseEvent()
        try:
            with patch.object(self.session.experiment_plans, 'shutdown') as plans:
                window.closeEvent(event)
                plans.assert_not_called()
            self.assertFalse(event.isAccepted())
        finally:
            self.exporter.can_shutdown = True


if __name__ == '__main__':
    unittest.main()
