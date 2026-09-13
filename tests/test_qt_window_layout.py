"""Title navigation, workspace tools and default listener behavior."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from flow_controller.core.session import FlowSession
from flow_controller.ui.qt_main_window import MainWindow


class WindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for name in ('segoeui.ttf', 'consola.ttf'):
            path = Path('C:/Windows/Fonts') / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        preferences = patch.dict(os.environ, {
            'FLOW_CONTROLLER_UNIT_PREFS': str(Path(directory.name) / 'units.json'),
            'FLOW_CONTROLLER_COMBUSTION_PREFS': str(Path(directory.name) / 'combustion.json'),
        })
        preferences.start()
        self.addCleanup(preferences.stop)
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        listener = patch.object(self.session, 'start_udp')
        self.start_udp = listener.start()
        self.addCleanup(listener.stop)
        self.window = MainWindow(self.session)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def test_navigation_is_centred_in_title_bar_and_switches_pages(self):
        window = self.window
        nav = window._nav_tabs
        self.assertTrue(window.title_bar.isAncestorOf(nav))
        self.assertTrue(window._tabs.tabBar().isHidden())
        centre = nav.mapTo(window.title_bar, nav.rect().center()).x()
        self.assertAlmostEqual(centre, window.title_bar.rect().center().x(), delta=2)
        nav.setCurrentIndex(1)
        self.assertIs(window._tabs.currentWidget(), window.operation_tab)
        self.assertIs(window._layout_stack.currentWidget(), window.operation_tab.panel_bar)
        window._tabs.setCurrentWidget(window.camera_tab)
        self.assertEqual(nav.currentIndex(), window._tabs.currentIndex())

    def test_layout_controls_move_to_row_below_title_and_survive_rebuild(self):
        window = self.window
        window._tabs.setCurrentWidget(window.logging_tab)
        self.assertTrue(window.layout_bar.isAncestorOf(window.logging_tab.controls_panel_btn))
        self.assertFalse(window.logging_tab.isAncestorOf(window.logging_tab.controls_panel_btn))
        window._rebuild()
        self.app.processEvents()
        self.assertIs(window._tabs.currentWidget(), window.logging_tab)
        self.assertEqual(window._nav_tabs.currentIndex(), 2)
        self.assertIs(window._layout_stack.currentWidget(), window.logging_tab.panel_bar)
        self.start_udp.assert_called_once_with('127.0.0.1', 61557)

    def test_listener_auto_start_happens_once_and_manual_stop_survives_rebuild(self):
        self.start_udp.assert_called_once_with('127.0.0.1', 61557)
        self.session.udp_changed.emit(False, 'Stopped')
        self.window._rebuild()
        self.app.processEvents()
        self.start_udp.assert_called_once()
        self.assertEqual(self.window.operation_tab.udp_btn.text(), 'Start Listener')

    def test_listener_failure_is_visible_and_leaves_a_retry_button(self):
        self.session.udp_changed.emit(False, 'Listener error: address already in use')
        self.app.processEvents()
        self.assertIn('address already in use', self.window._message_label.text())
        self.assertTrue(self.window._message_label.isVisible())
        self.assertEqual(self.window.operation_tab.udp_btn.text(), 'Start Listener')


if __name__ == '__main__':
    unittest.main()
