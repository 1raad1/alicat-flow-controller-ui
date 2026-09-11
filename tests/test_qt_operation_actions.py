"""Regression tests for state-aware controls on the Operation tab."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QPushButton

from flow_controller.core.session import FlowSession
from flow_controller.ui.qt_operation_tab import OperationTab
from flow_controller.ui.qt_widgets import Card


class QtOperationActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
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

    def _session(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        return session

    def _tab(self, session, **kwargs):
        tab = OperationTab(session, **kwargs)
        self.addCleanup(tab.close)
        return tab

    def test_camera_is_first_left_card_and_layout_actions_share_panel_bar(self):
        camera = object()
        with patch('flow_controller.ui.qt_camera.BurnerCameraCard',
                   side_effect=lambda _camera: Card('DSLR Camera')):
            tab = self._tab(self._session(), camera=camera)

        cards_layout = tab.camera_card.parentWidget().layout()
        self.assertIs(cards_layout.itemAt(0).widget(), tab.camera_card)
        self.assertEqual(
            cards_layout.itemAt(1).widget()._title_label.text(),
            'Logging & Acquisition')

        self.assertIs(tab.layout().itemAt(0).widget(), tab.panel_bar)
        for button in tab._cards_view_buttons.values():
            self.assertIs(button.parentWidget(), tab.panel_bar)
        self.assertTrue(tab._cards_view_buttons[tab._cards_view].isChecked())

    def test_logging_uses_one_state_aware_button_and_dispatches_both_actions(self):
        session = self._session()
        session.resolve_log_path = Mock(return_value=(
            Path('shown.csv'), Path('actual.csv')))
        session.start_logging = Mock()
        session.stop_logging = Mock()
        tab = self._tab(session)

        logging_buttons = [
            button for button in tab.findChildren(QPushButton)
            if button.text() in ('Start Logging', 'Stop Logging')
        ]
        self.assertEqual(logging_buttons, [tab.logging_btn])
        self.assertIs(tab.start_log_btn, tab.logging_btn)

        tab.logging_btn.click()
        session.start_logging.assert_called_once_with(Path('actual.csv'))
        session.stop_logging.assert_not_called()

        session.logging_changed.emit(True, Path('external.csv'))
        self.assertEqual(tab.logging_btn.text(), 'Stop Logging')
        self.assertFalse(tab.log_path.isEnabled())
        tab.logging_btn.click()
        session.stop_logging.assert_called_once_with()

        session.logging_changed.emit(False, Path('external.csv'))
        self.assertEqual(tab.logging_btn.text(), 'Start Logging')
        self.assertTrue(tab.log_path.isEnabled())

    def test_inactive_listener_uses_labview_default_address(self):
        tab = self._tab(self._session())
        self.assertEqual(tab.udp_host.text(), '127.0.0.1')
        self.assertEqual(tab.udp_port.text(), '61557')
        self.assertEqual(tab.udp_btn.text(), 'Start Listener')

    def test_udp_initial_state_uses_listener_and_dispatch_does_not_parse_label(self):
        session = self._session()
        session._udp.host = '192.0.2.25'
        session._udp.port = 62001
        session._udp._socket = object()
        self.addCleanup(setattr, session._udp, '_socket', None)
        session.stop_udp = Mock()
        session.start_udp = Mock()
        tab = self._tab(session)

        self.assertEqual(tab.udp_host.text(), '192.0.2.25')
        self.assertEqual(tab.udp_port.text(), '62001')
        self.assertEqual(tab.udp_btn.text(), 'Stop Listener')
        self.assertEqual(tab.udp_state.text(),
                         'Listening on 192.0.2.25:62001')

        tab.udp_btn.setText('Start Listener')
        tab.udp_btn.click()
        session.stop_udp.assert_called_once_with()
        session.start_udp.assert_not_called()

        tab._on_udp(False, 'listener off')
        tab.udp_btn.setText('Stop Listener')
        tab.udp_btn.click()
        session.start_udp.assert_called_once_with('192.0.2.25', 62001)


if __name__ == '__main__':
    unittest.main()
