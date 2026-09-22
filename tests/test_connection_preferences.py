"""Last-used choices survive new sessions without reusing hardware discovery."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication
from flow_controller.core.session import FlowSession
from flow_controller.domain.models import ControllerInfo, DiscoveryResult
from flow_controller.ui.qt_connection_tab import ConnectionTab


class ConnectionPreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'connection.json'
        env = patch.dict(os.environ, {
            'FLOW_CONTROLLER_CONNECTION_PREFS': str(self.path),
            'FLOW_CONTROLLER_UNIT_PREFS': str(Path(directory.name) / 'units.json'),
            'FLOW_CONTROLLER_COMBUSTION_PREFS': str(Path(directory.name) / 'combustion.json'),
        })
        env.start()
        self.addCleanup(env.stop)

    def tab(self, ports=('COM3', 'COM9')):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        with patch('flow_controller.core.session.serial.tools.list_ports.comports',
                   return_value=[SimpleNamespace(device=p) for p in ports]):
            tab = ConnectionTab(session)
        self.addCleanup(tab.close)
        return tab

    def scan(self, tab):
        result = DiscoveryResult([
            ControllerInfo('A', {'gas': 'Air'}, {0: 'Air', 1: 'H2'}),
            ControllerInfo('B', {'gas': 'Air'}, {0: 'Air'}),
        ], port=tab._port(), baudrate=tab._baudrate())
        tab.session.last_scan = result
        tab._on_scan_finished(result)

    def test_port_and_baud_survive_restart_and_missing_port_refresh(self):
        first = self.tab()
        first.port_combo.setCurrentText('COM9')
        first.baud_combo.setCurrentText('19200')
        second = self.tab()
        self.assertEqual((second._port(), second._baudrate()), ('COM9', 19200))
        second._on_ports([])
        second._on_ports(['COM3'])
        second._on_ports(['COM3', 'COM9'])
        self.assertEqual(second._port(), 'COM9')
        self.assertEqual(self.tab()._port(), 'COM9')
        self.assertFalse(second.session.controllers_connected)
        self.assertIsNone(second.session.last_scan)

    def test_assignments_restore_after_scan_and_are_scoped_to_port(self):
        first = self.tab()
        first.port_combo.setCurrentText('COM9')
        self.scan(first)
        first._rows['A'].choose_gas('H2')
        first._rows['A'].zone_combo.setCurrentText('Zone 2')
        first._rows['B'].set_included(False)
        second = self.tab()
        self.assertEqual(second.session.selection, {})
        self.scan(second)
        self.assertEqual(second._rows['A'].selection(), ('H2', 'Zone 2'))
        self.assertFalse(second._rows['B'].included)
        self.assertNotIn('B', second.session.selection)
        second.port_combo.setCurrentText('COM3')
        self.scan(second)
        self.assertEqual(second._rows['A'].gas(), 'Air')
        self.assertTrue(second._rows['B'].included)

    def test_corrupt_preferences_do_not_prevent_startup(self):
        self.path.write_text('{broken', encoding='utf-8')
        tab = self.tab()
        self.assertEqual(tab._port(), 'COM3')
        self.assertGreater(tab._baudrate(), 0)

    def test_refresh_with_missing_port_does_not_replace_saved_choice(self):
        first = self.tab()
        first.port_combo.setCurrentText('COM9')
        fallback = self.tab(ports=('COM3',))
        self.assertEqual(fallback._port(), 'COM3')
        self.assertEqual(self.tab()._port(), 'COM9')

    def test_all_excluded_survives_restart_and_rescan(self):
        first = self.tab()
        self.scan(first)
        first._set_all_included(False)
        second = self.tab()
        self.scan(second)
        self.assertTrue(all(not row.included for row in second._rows.values()))
        self.assertEqual(second.session.selection, {})

    def test_rebuilding_tab_preserves_all_excluded(self):
        first = self.tab()
        self.scan(first)
        first._set_all_included(False)
        with patch('flow_controller.core.session.serial.tools.list_ports.comports',
                   return_value=[SimpleNamespace(device='COM3')]):
            rebuilt = ConnectionTab(first.session)
        self.addCleanup(rebuilt.close)
        self.assertTrue(all(not row.included for row in rebuilt._rows.values()))
        self.assertEqual(first.session.selection, {})
