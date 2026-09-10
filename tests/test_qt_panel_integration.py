"""Integration regressions for the collapsible operation and logging panels."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from flow_controller.core.session import FlowSession
from flow_controller.ui import qt_theme as theme
from flow_controller.ui.qt_logging_tab import LoggingTab
from flow_controller.ui.qt_operation_tab import OperationTab


class QtPanelIntegrationTests(unittest.TestCase):
    """Exercise panel controls through the same signals used by the UI."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # Windows' offscreen platform does not discover system fonts itself.
        # Real glyph metrics catch controls clipped by an undersized pane.
        for filename in ('segoeui.ttf', 'consola.ttf'):
            font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / filename
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        # Several controls persist values as soon as they are changed.  Keep
        # these integration checks away from the installed rig preferences.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        preferences = patch.dict(os.environ, {
            'FLOW_CONTROLLER_UNIT_PREFS': str(
                Path(directory.name) / 'units.json'),
            'FLOW_CONTROLLER_COMBUSTION_PREFS': str(
                Path(directory.name) / 'combustion.json'),
        })
        preferences.start()
        self.addCleanup(preferences.stop)

    def _show(self, widget):
        widget.setStyleSheet(theme.STYLESHEET)
        widget.resize(1560, 900)
        widget.show()
        self._settle()
        self.addCleanup(self._dispose, widget)
        return widget

    def _settle(self):
        QTest.qWait(260)
        self.app.processEvents()

    def _dispose(self, widget):
        widget.close()
        widget.deleteLater()
        self.app.processEvents()

    def _session(self):
        session = FlowSession(
            worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        return session

    @staticmethod
    def _reset_button(widget):
        return next(button for button in widget.findChildren(QPushButton)
                    if button.text() == 'Reset layout')

    def test_operation_controls_toggle_preserves_both_columns(self):
        session = self._session()
        tab = self._show(OperationTab(session))
        controls = tab._columns_splitter.widget(0)
        live_view = tab._columns_splitter.widget(1)

        self.assertTrue(tab.controls_panel_btn.isChecked())
        self.assertGreater(tab._columns_splitter.sizes()[0], 0)
        self.assertGreater(live_view.geometry().width(), 0)
        self.assertGreaterEqual(controls.width(),
                                controls.content.minimumSizeHint().width())

        QTest.mouseClick(tab.controls_panel_btn,
                         Qt.MouseButton.LeftButton)
        self._settle()
        self.assertFalse(tab.controls_panel_btn.isChecked())
        self.assertEqual(tab._columns_splitter.sizes()[0], 0)
        self.assertGreater(tab._columns_splitter.sizes()[1], 0)
        self.assertGreater(live_view.geometry().width(), 0)

        QTest.mouseClick(tab.controls_panel_btn,
                         Qt.MouseButton.LeftButton)
        self._settle()
        self.assertTrue(tab.controls_panel_btn.isChecked())
        self.assertGreater(tab._columns_splitter.sizes()[0], 0)
        self.assertIs(tab._columns_splitter.widget(0), controls)
        self.assertIs(tab._columns_splitter.widget(1), live_view)
        self.assertIs(tab.session, session)
        self.assertTrue(live_view.isEnabled())
        self.assertGreater(live_view.geometry().width(), 0)

    def test_operation_sequence_controls_and_handle_stay_synchronized(self):
        tab = self._show(OperationTab(self._session()))

        self.assertFalse(tab.sequence_panel_btn.isChecked())
        self.assertFalse(tab.sequence_btn.isChecked())
        self.assertEqual(tab._split.sizes()[1], 0)

        QTest.mouseClick(tab.sequence_panel_btn,
                         Qt.MouseButton.LeftButton)
        self._settle()
        self.assertTrue(tab.sequence_panel_btn.isChecked())
        self.assertTrue(tab.sequence_btn.isChecked())
        self.assertGreater(tab._split.sizes()[1], 0)
        self.assertGreater(tab._split.sizes()[0], 0)
        self.assertGreater(tab.sequence_panel.geometry().height(), 0)

        QTest.mouseClick(tab.sequence_btn, Qt.MouseButton.LeftButton)
        self._settle()
        self.assertFalse(tab.sequence_panel_btn.isChecked())
        self.assertFalse(tab.sequence_btn.isChecked())
        self.assertEqual(tab._split.sizes()[1], 0)

        tab._split.set_panel_collapsed(1, False)
        self._settle()
        self.assertTrue(tab.sequence_panel_btn.isChecked())
        self.assertTrue(tab.sequence_btn.isChecked())
        self.assertGreater(tab._split.sizes()[1], 0)

        handle = tab._split.handle(1)
        handle.setFocus()
        QTest.keyClick(handle, Qt.Key.Key_Enter)
        self._settle()
        self.assertFalse(tab.sequence_panel_btn.isChecked())
        self.assertFalse(tab.sequence_btn.isChecked())
        self.assertEqual(tab._split.sizes()[1], 0)

    def test_operation_reset_restores_controls_and_closes_sequence(self):
        tab = self._show(OperationTab(self._session()))
        tab._columns_splitter.set_panel_collapsed(0, True)
        tab._split.set_panel_collapsed(1, False)
        self._settle()
        self.assertFalse(tab.controls_panel_btn.isChecked())
        self.assertTrue(tab.sequence_panel_btn.isChecked())

        QTest.mouseClick(self._reset_button(tab),
                         Qt.MouseButton.LeftButton)
        self._settle()
        self.assertTrue(tab.controls_panel_btn.isChecked())
        self.assertFalse(tab.sequence_panel_btn.isChecked())
        self.assertFalse(tab.sequence_btn.isChecked())
        self.assertTrue(tab._columns_splitter.widget(0).isEnabled())
        self.assertTrue(tab._columns_splitter.content_widget(0).isEnabled())
        self.assertGreater(tab._columns_splitter.sizes()[0], 0)
        self.assertGreater(tab._columns_splitter.sizes()[1], 0)
        self.assertEqual(tab._split.sizes()[1], 0)

    def test_logging_controls_button_and_reset_follow_splitter(self):
        tab = self._show(LoggingTab(self._session()))
        controls = tab._split.widget(0)
        plots = tab._split.widget(1)

        self.assertTrue(tab.controls_panel_btn.isChecked())
        self.assertGreater(tab._split.sizes()[0], 0)
        tab._split.set_panel_collapsed(0, True)
        self._settle()
        self.assertFalse(tab.controls_panel_btn.isChecked())
        self.assertEqual(tab._split.sizes()[0], 0)
        self.assertGreater(plots.geometry().width(), 0)

        QTest.mouseClick(tab.controls_panel_btn,
                         Qt.MouseButton.LeftButton)
        self._settle()
        self.assertTrue(tab.controls_panel_btn.isChecked())
        self.assertGreater(tab._split.sizes()[0], 0)
        self.assertIs(tab._split.widget(0), controls)
        self.assertIs(tab._split.widget(1), plots)

        tab._split.set_panel_collapsed(0, True)
        self._settle()
        QTest.mouseClick(self._reset_button(tab),
                         Qt.MouseButton.LeftButton)
        self._settle()
        self.assertTrue(tab.controls_panel_btn.isChecked())
        self.assertGreater(tab._split.sizes()[0], 0)
        self.assertGreater(tab._split.sizes()[1], 0)
        self.assertTrue(controls.isEnabled())
        self.assertTrue(tab._split.content_widget(0).isEnabled())
        self.assertGreater(plots.geometry().width(), 0)


if __name__ == '__main__':
    unittest.main()
