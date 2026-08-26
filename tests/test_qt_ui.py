"""Regression checks for the persistent Qt controls and appearance settings."""

from __future__ import annotations

import os
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QWidget

from flow_controller.core.combustion_prefs import (GEOMETRY_AREA,
                                                   SCOPE_STAGE2)
from flow_controller.core.session import (FlowSession, MODE_STAGED,
                                          MODE_STANDARD)
from flow_controller.core.sequence import Sequence
from flow_controller.ui import qt_config
from flow_controller.ui.qt_main_window import MainWindow
from flow_controller.ui.qt_operation_tab import (
    OperationTab, SafetyBar, _area_from_diameter, _sequence_stem,
)
from flow_controller.ui.qt_settings import SettingsDialog
from flow_controller.ui.qt_widgets import Card


class _SafetySession(QObject):
    estop_armed_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.estop_armed = True
        self.zero_fuel_calls = 0
        self.zero_all_calls = 0

    def zero_fuel(self):
        self.zero_fuel_calls += 1

    def zero_all(self):
        self.zero_all_calls += 1


class QtUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_connected_badge_counts_assigned_controllers(self):
        session = SimpleNamespace(
            assigned_units=lambda: ['A', 'B'], controller_instances={},
            port='COM7', baudrate=57600)

        class Harness:
            def __init__(self):
                self.session = session
                self.link = None

            def _set_link(self, kind, text):
                self.link = kind, text

            def _settle_pending_theme(self):
                pass

        harness = Harness()
        MainWindow._on_connection(harness, True)
        self.assertEqual(
            harness.link,
            ('ok', '2 controllers · COM7 · 57600 baud'))

    def test_top_bar_owns_batch_and_zero_actions(self):
        session = _SafetySession()
        sent = []
        bar = SafetyBar(session, lambda: sent.append(True))

        self.assertEqual(
            list(bar.buttons), ['SET ALL FLOWS', 'ZERO FUEL', 'ZERO ALL'])
        bar.buttons['SET ALL FLOWS'].click()
        bar.buttons['ZERO FUEL'].click()
        bar.buttons['ZERO ALL'].click()
        self.assertEqual(sent, [True])
        self.assertEqual(session.zero_fuel_calls, 1)
        self.assertEqual(session.zero_all_calls, 1)

        session.estop_armed_changed.emit(False)
        self.assertTrue(all(not button.isEnabled()
                            for button in bar.buttons.values()))

    def test_font_settings_are_dropdowns_with_dm_sans_default(self):
        config = qt_config.defaults()
        self.assertEqual(config['font']['ui_family'], 'DM Sans')

        dialog = SettingsDialog(config)
        pickers = dialog.findChildren(QComboBox)
        self.assertEqual(len(pickers), 2)
        self.assertTrue(all(not picker.isEditable() for picker in pickers))
        self.assertIn('DM Sans', [picker.currentText() for picker in pickers])
        dialog.close()

    def test_standard_is_the_startup_default_and_connection_keeps_it(self):
        worker = SimpleNamespace(shutdown=lambda: None)
        session = FlowSession(worker=worker)
        self.assertEqual(session.operating_mode, MODE_STANDARD)

        session.selection = {
            'A': ('NH3', 'Zone 1'), 'B': ('H2', 'Zone 1'),
            'C': ('Air', 'Zone 1'), 'D': ('NH3', 'Zone 2'),
            'E': ('H2', 'Zone 2'), 'F': ('Air', 'Zone 2'),
            'G': ('CH4', 'Pilot'),
        }
        session._rebuild_assignments()
        session.autocalc_available = True
        confirmed = {
            unit: {'gas': gas} for unit, (gas, _zone)
            in session.selection.items()
        }
        session._finish_connect(
            SimpleNamespace(result=lambda: (confirmed, {})))

        self.assertEqual(session.operating_mode, MODE_STANDARD)
        self.assertTrue(session.set_operating_mode(MODE_STAGED))
        self.assertEqual(session.operating_mode, MODE_STAGED)
        session.shutdown()

    def test_staged_combustion_is_grouped_by_pilot_and_stage(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        tab = OperationTab(session)
        self.assertFalse(hasattr(tab, '_ignition_card'))
        self.assertIsInstance(tab._sequence_card, Card)
        self.assertNotIsInstance(tab._experiment_plan_card, Card)
        self.assertEqual(tab._sequence_card._title_label.text(), 'Sequences')
        self.assertEqual(tab._sequence_card._badge.text(), '')

        groups = tab._combustion_card.findChildren(
            QWidget, 'CombustionStageCard')
        self.assertEqual(len(groups), 3)
        titles = tab._combustion_card.findChildren(
            QWidget, 'CombustionGroupTitle')
        self.assertEqual([title.text() for title in titles[:3]],
                         ['PILOT', 'STAGE 1', 'STAGE 2'])
        self.assertEqual(set(tab._combustion), {
            'pilot_split', 'phi1', 'vel1', 'power1',
            'phi2', 'vel2', 'power2',
        })
        self.assertEqual(set(tab._combustion_std), {'phi', 'vel', 'power'})
        self.assertEqual(len(tab.findChildren(
            QWidget, 'CombustionGeometryInput')), 3)
        self.assertEqual(len(tab.findChildren(
            QWidget, 'CombustionGeometryMode')), 3)
        self.assertEqual(len(tab.findChildren(
            QWidget, 'CombustionInletCountInput')), 1)
        self.assertEqual(len(tab.findChildren(QWidget, 'CardMenuButton')), 2)
        self.assertTrue(all(not menu.isVisible()
                            for menu in tab._combustion_menus))
        session.combustion_prefs['stage1_mm'] = 20.0
        session.combustion_prefs['stage2_mm'] = 20.0
        session.combustion_prefs['stage1_geometry'] = 'diameter'
        session.combustion_prefs['stage2_geometry'] = 'diameter'
        session.combustion_prefs['stage2_inlets'] = 4
        tab._apply_combustion_prefs()
        self.assertIn('314.2',
                      tab._combustion_area_labels['stage1'][0].text())
        self.assertIn('1257',
                      tab._combustion_area_labels['stage2'][0].text())
        self.assertAlmostEqual(
            session.combustion_effective_diameter(SCOPE_STAGE2), 40.0)
        session.combustion_prefs['stage2_geometry'] = GEOMETRY_AREA
        session.combustion_prefs['stage2_area_mm2'] = 400.0
        tab._apply_combustion_prefs()
        self.assertIn('1600',
                      tab._combustion_area_labels['stage2'][0].text())
        self.assertAlmostEqual(
            session.combustion_effective_diameter(SCOPE_STAGE2),
            math.sqrt(4.0 * 1600.0 / math.pi))
        tab._refresh_combustion_staged({}, flows={
            'ch4_pilot': 1.0, 'nh3_rich': 7.0, 'h2_rich': 2.0,
        })
        self.assertEqual(tab._combustion['pilot_split'].value.text(), '10.0')
        self.assertTrue(tab._combustion_card.isHidden())

        session.set_operating_mode(MODE_STAGED)
        self.assertFalse(tab._combustion_card.isHidden())
        tab.close()
        session.shutdown()

    def test_controller_cards_toggle_between_list_and_grid(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        session.selection = {
            'A': ('NH3', 'Zone 1'),
            'B': ('Air', 'Zone 1'),
            'C': ('H2', 'Zone 1'),
        }
        session._rebuild_assignments()
        tab = OperationTab(session)

        self.assertEqual(tab._cards_view, 'list')
        self.assertTrue(tab._cards_view_buttons['list'].isChecked())
        self.assertTrue(all(not card._compact
                            for card in tab._cards.values()))
        self.assertTrue(all(not card.settings_button.isHidden()
                            for card in tab._cards.values()))
        self.assertTrue(all(not card.settings_menu.isVisible()
                            for card in tab._cards.values()))
        self.assertTrue(all(card.settings_menu.isAncestorOf(card.scale_spin)
                            and card.settings_menu.isAncestorOf(card.max_flow_spin)
                            and card.settings_menu.isAncestorOf(card.ramp_spin)
                            for card in tab._cards.values()))
        self.assertTrue(all(card.ramp_off_btn.isChecked()
                            for card in tab._cards.values()))
        tab._cards['A'].entry.setText('1.25')

        tab._cards_view_buttons['grid'].click()

        self.assertEqual(tab._cards_view, 'grid')
        self.assertEqual(session.controller_cards_view, 'grid')
        self.assertTrue(tab._cards_view_buttons['grid'].isChecked())
        self.assertFalse(tab._cards_view_buttons['list'].isChecked())
        self.assertTrue(all(card._compact for card in tab._cards.values()))
        self.assertEqual(tab._cards['A'].entry.text(), '1.25')
        positions = {}
        for unit, card in tab._cards.items():
            index = tab._cards_layout.indexOf(card)
            row_number, column, _row_span, _column_span = (
                tab._cards_layout.getItemPosition(index))
            positions[unit] = row_number, column
        occupied_rows = {}
        for row_number, column in positions.values():
            occupied_rows.setdefault(row_number, set()).add(column)
        self.assertEqual(sorted(map(len, occupied_rows.values())), [1, 2])
        self.assertIn({0, 1}, occupied_rows.values())

        tab._cards_view_buttons['list'].click()
        self.assertTrue(all(not card._compact
                            for card in tab._cards.values()))
        tab.close()
        session.shutdown()

    def test_agent_terminal_is_mounted_in_operation_sidebar(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        window = MainWindow(session)
        self.assertIs(window.agent_pane, window.operation_tab.agent_pane)
        self.assertTrue(window.operation_tab.isAncestorOf(window.agent_pane))
        self.assertTrue(window.agent_pane.terminal.isReadOnly())
        window.close()
        self.app.processEvents()

    def test_window_refuses_to_close_if_agent_cannot_be_terminated(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        window = MainWindow(session)
        window.agent_manager.shutdown = Mock(return_value=False)
        event = Mock()

        with patch.object(QMessageBox, 'critical') as warning:
            window.closeEvent(event)

        event.ignore.assert_called_once_with()
        warning.assert_called_once()
        window.agent_manager.shutdown = Mock(return_value=True)
        window.close()
        self.app.processEvents()

    def test_inlet_area_is_calculated_from_the_diameter(self):
        self.assertAlmostEqual(_area_from_diameter(20.0), math.pi * 100.0)
        self.assertIsNone(_area_from_diameter(None))

    def test_saved_sequence_rename_updates_file_metadata_and_open_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            original = Sequence(name='old name', notes='saved').save(
                folder / 'old name.fcseq.json')
            session = FlowSession(
                worker=SimpleNamespace(shutdown=lambda: None))
            session.sequence_dir = folder
            session.sequence = Sequence.load(original)
            session.sequence.notes = 'unsaved panel edit'
            tab = OperationTab(session)

            self.assertTrue(tab._rename_saved_to(original, 'new name'))
            destination = folder / 'new name.fcseq.json'
            self.assertFalse(original.exists())
            self.assertTrue(destination.exists())
            self.assertEqual(Sequence.load(destination).name, 'new name')
            self.assertEqual(session.sequence.name, 'new name')
            self.assertEqual(session.sequence.path, destination)
            self.assertEqual(session.sequence.notes, 'unsaved panel edit')
            current_row = tab.saved_list.itemWidget(tab.saved_list.item(0))
            self.assertIsNotNone(
                current_row.findChild(QWidget, 'RowRename'))

            Sequence(name='already there').save(
                folder / 'already there.fcseq.json')
            self.assertFalse(
                tab._rename_saved_to(destination, 'already there'))
            self.assertTrue(destination.exists())

            tab.close()
            session.shutdown()

    def test_saved_sequence_names_reject_unsafe_filenames(self):
        self.assertEqual(_sequence_stem('  useful run.fcseq.json  '),
                         'useful run')
        for name in ('', 'bad/name', 'CON', 'trailing.'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    _sequence_stem(name)


if __name__ == '__main__':
    unittest.main()
