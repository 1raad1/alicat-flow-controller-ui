"""Hardware-free checks for the direct-USB camera interface."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPushButton

from flow_controller.core.session import FlowSession
from flow_controller.ui import qt_theme as theme
from flow_controller.ui.qt_camera import BurnerCameraCard, CameraImage, CameraTab
from flow_controller.ui.qt_operation_tab import OperationTab


class FakeCamera(QObject):
    frame_received = Signal(QImage)
    status_changed = Signal(str)
    error = Signal(str)
    busy_changed = Signal(bool)
    state_changed = Signal(dict)
    captured = Signal(str)
    action_finished = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.state = {
            'cameras': [], 'selected': '', 'capabilities': [],
            'properties': [], 'battery': None,
        }
        self.busy = False
        self.previewing = False
        self.calls = []

    def connect_camera(self):
        self.calls.append(('connect_camera',))

    def select_camera(self, identifier):
        self.calls.append(('select_camera', identifier))

    def action(self, action_name, **parameters):
        self.calls.append(('action', action_name, parameters))
        return True

    def start_preview(self):
        self.calls.append(('start_preview',))

    def stop_preview(self):
        self.calls.append(('stop_preview',))

    def set_fps(self, value):
        self.calls.append(('set_fps', value))

    def set_output_directory(self, path):
        self.calls.append(('set_output_directory', path))

    def shutdown(self):
        self.calls.append(('shutdown',))
        return True

    def publish(self, **changes):
        self.state = {**self.state, **changes}
        self.state_changed.emit(dict(self.state))


class QtCameraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat,
                          QSettings.Scope.UserScope, cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        settings = QSettings('FlowController', 'Camera')
        settings.clear()
        settings.sync()

    @staticmethod
    def button(widget, text):
        return next(button for button in widget.findChildren(QPushButton)
                    if button.text() == text)

    def make_tab(self):
        camera = FakeCamera()
        tab = CameraTab(camera)
        self.addCleanup(tab.close)
        self.addCleanup(tab.shutdown)
        return camera, tab

    def test_construction_only_applies_safe_preferences(self):
        settings = QSettings('FlowController', 'Camera')
        settings.setValue('fps', 15)
        settings.setValue('output', 'C:/camera-output')
        camera, tab = self.make_tab()

        self.assertEqual(camera.calls, [
            ('set_fps', 15),
            ('set_output_directory', 'C:/camera-output'),
        ])
        self.assertEqual(tab.fps.currentText(), '15')
        self.assertEqual(tab.output_path.text(), 'C:/camera-output')

        camera.calls.clear()
        tab.output_path.setText('C:/edited-output')
        tab.output_path.editingFinished.emit()
        self.assertEqual(camera.calls,
                         [('set_output_directory', 'C:/edited-output')])
        self.assertEqual(
            QSettings('FlowController', 'Camera').value('output'),
            'C:/edited-output',
        )

    def test_shared_frames_clear_and_popout_does_not_stop_preview(self):
        camera = FakeCamera()
        first, second = BurnerCameraCard(camera), BurnerCameraCard(camera)
        self.addCleanup(first.shutdown)
        self.addCleanup(second.shutdown)
        image = QImage(32, 18, QImage.Format.Format_RGB32)
        image.fill(QColor('#d45a32'))

        camera.frame_received.emit(image)
        self.assertEqual(first.preview.image.size(), image.size())
        self.assertEqual(second.preview.image.pixelColor(0, 0), QColor('#d45a32'))
        first.pop_out()
        popout = first.popout
        self.assertIsNotNone(popout)
        self.assertIsInstance(popout.findChild(CameraImage), CameraImage)
        popout.close()
        self.app.processEvents()
        first.pop_out()
        self.assertIs(first.popout, popout)
        self.assertNotIn(('stop_preview',), camera.calls)

        camera.frame_received.emit(QImage())
        self.assertTrue(first.preview.image.isNull())
        self.assertTrue(second.preview.image.isNull())

    def test_compact_card_gates_and_dispatches_native_controls(self):
        camera = FakeCamera()
        card = BurnerCameraCard(camera)
        self.addCleanup(card.shutdown)
        self.assertFalse(card.start_button.isEnabled())
        self.assertFalse(card.capture_button.isEnabled())

        camera.publish(selected='usb:1', capabilities=['LiveView'])
        self.assertTrue(card.start_button.isEnabled())
        self.assertTrue(card.capture_button.isEnabled())
        card.start_button.click()
        card.capture_button.click()
        self.assertIn(('start_preview',), camera.calls)
        self.assertIn(('action', 'capture', {}), camera.calls)

        camera.previewing = True
        camera.publish()
        self.assertTrue(card.stop_button.isEnabled())
        card.stop_button.click()
        self.assertIn(('stop_preview',), camera.calls)
        camera.busy_changed.emit(True)
        self.assertFalse(card.start_button.isEnabled())
        self.assertFalse(card.capture_button.isEnabled())
        camera.busy_changed.emit(False)
        camera.publish(workflow='timelapse')
        self.assertFalse(card.start_button.isEnabled())
        self.assertFalse(card.capture_button.isEnabled())
        self.assertTrue(card.stop_button.isEnabled())

    def test_discovery_selection_disconnect_and_capability_gating(self):
        camera, tab = self.make_tab()
        camera.calls.clear()
        tab.discover_button.click()
        self.assertEqual(camera.calls.pop(), ('connect_camera',))

        camera.publish(
            cameras=[{'id': 'usb:a', 'name': 'Alpha'},
                     {'id': 'usb:b', 'name': 'Beta'}],
            selected='usb:a',
            capabilities=['CaptureNoAf', 'LiveView', 'SimpleManualFocus'],
            battery=73,
        )
        self.assertEqual(tab.cameras.itemData(1), 'usb:b')
        self.assertEqual(tab.cameras.currentText(), 'Alpha')
        self.assertEqual(tab.battery.text(), 'Battery 73%')
        self.assertTrue(tab.capture_no_af_button.isEnabled())
        self.assertTrue(tab.focus_plus_button.isEnabled())
        self.assertFalse(tab.video_start_button.isEnabled())
        tab._select_camera(1)
        tab.disconnect_button.click()
        self.assertEqual(camera.calls[-2:], [
            ('select_camera', 'usb:b'),
            ('action', 'disconnect', {}),
        ])

        camera.busy_changed.emit(True)
        self.assertFalse(tab.discover_button.isEnabled())
        self.assertFalse(tab.cameras.isEnabled())
        self.assertFalse(tab.capture_button.isEnabled())

    def test_capture_video_focus_bulb_and_focus_point_dispatch(self):
        camera, tab = self.make_tab()
        camera.publish(selected='usb:1', capabilities=[
            'CaptureNoAf', 'LiveView', 'RecordMovie', 'Bulb',
            'SimpleManualFocus', 'CanLockFocus',
        ])
        camera.calls.clear()
        tab.focus_step.setCurrentText('3')
        for button in (
            tab.capture_button, tab.capture_no_af_button,
            tab.live_start_button, tab.live_stop_button,
            tab.video_start_button, tab.video_stop_button,
            tab.bulb_start_button, tab.bulb_stop_button,
            tab.lock_button, tab.unlock_button,
            tab.autofocus_button, tab.focus_minus_button,
            tab.focus_plus_button,
        ):
            button.click()
        tab.focus_x.setValue(250)
        tab.focus_y.setValue(750)
        tab.focus_point_button.click()
        self.assertEqual(camera.calls, [
            ('action', 'capture', {}),
            ('action', 'capture_no_af', {}),
            ('start_preview',),
            ('stop_preview',),
            ('action', 'video_start', {}),
            ('action', 'video_stop', {}),
            ('action', 'bulb_start', {}),
            ('action', 'bulb_stop', {}),
            ('action', 'camera_lock', {}),
            ('action', 'camera_unlock', {}),
            ('action', 'autofocus', {}),
            ('action', 'focus', {'step': -3}),
            ('action', 'focus', {'step': 3}),
            ('action', 'focus_point', {'x': 250, 'y': 750}),
        ])

    def test_properties_build_editors_and_apply_exact_values(self):
        camera, tab = self.make_tab()
        camera.publish(
            selected='usb:1', capabilities=['LiveView'],
            properties=[
                {'name': 'iso', 'label': 'ISO', 'value': '200',
                 'values': ['100', '200', '400'], 'readonly': False},
                {'name': 'serial', 'label': 'Serial', 'value': 'ABC',
                 'values': [], 'readonly': True},
            ],
        )
        self.assertEqual(tab.properties_table.rowCount(), 2)
        iso = tab._property_editors['iso']
        self.assertIsInstance(iso, QComboBox)
        tab.refresh_properties_button.click()
        self.assertEqual(camera.calls[-1], ('action', 'refresh', {}))
        iso.setCurrentText('400')
        tab.properties_table.cellWidget(0, 3).click()
        self.assertEqual(camera.calls[-1],
                         ('action', 'set_property', {'name': 'iso', 'value': '400'}))
        self.assertIsInstance(tab._property_editors['serial'], QLineEdit)
        self.assertFalse(tab.properties_table.cellWidget(1, 3).isEnabled())

    def test_workflows_dispatch_typed_parameters(self):
        camera, tab = self.make_tab()
        camera.publish(
            selected='usb:1', capabilities=['CaptureNoAf'],
            properties=[{'name': 'shutterspeed', 'value': '1/60',
                         'values': ['1/30', '1/60', '1/125'],
                         'readonly': False}],
        )
        camera.calls.clear()
        tab.interval.setValue(2.5)
        tab.count.setValue(12)
        tab.timelapse_af.setChecked(True)
        tab.timelapse_button.click()
        tab.bracket_property.setText('shutterspeed')
        tab.bracket_values.setText('1/30, 1/60, 1/125')
        tab.bracket_af.setChecked(False)
        tab.bracket_button.click()
        camera.publish(workflow='bracket')
        tab.stop_workflow_button.click()
        self.assertEqual(camera.calls, [
            ('action', 'timelapse_start',
             {'interval': 2.5, 'count': 12, 'autofocus': True}),
            ('action', 'bracket_start',
             {'property': 'shutterspeed',
              'values': ['1/30', '1/60', '1/125'], 'autofocus': False}),
            ('action', 'workflow_stop', {}),
        ])

    def test_capture_history_status_and_shutdown(self):
        camera, tab = self.make_tab()
        camera.captured.emit('C:/captures/flame.jpg')
        self.assertEqual(tab.recent.item(0).text(), 'C:/captures/flame.jpg')
        self.assertTrue(tab.open_capture_button.isEnabled())
        camera.action_finished.emit('timelapse', '3 of 10')
        self.assertEqual(tab.connection_status.text(), 'Timelapse: 3 of 10')
        camera.action_finished.emit('refresh', {'properties': ['large snapshot']})
        self.assertEqual(tab.connection_status.text(), 'Refresh completed')
        camera.error.emit('Camera disconnected')
        self.assertEqual(tab.connection_status.text(), 'Camera disconnected')
        self.assertTrue(tab.shutdown())
        self.assertNotIn(('shutdown',), camera.calls)

    def test_workflow_state_blocks_new_actions_but_keeps_stop_available(self):
        camera, tab = self.make_tab()
        camera.publish(selected='usb:1', capabilities=['LiveView'],
                       workflow='timelapse')
        self.assertFalse(tab.cameras.isEnabled())
        self.assertFalse(tab.capture_button.isEnabled())
        self.assertFalse(tab.refresh_properties_button.isEnabled())
        self.assertFalse(tab.output_path.isEnabled())
        self.assertTrue(tab.stop_workflow_button.isEnabled())
        camera.busy_changed.emit(True)
        self.assertTrue(tab.stop_workflow_button.isEnabled())

    def test_capture_in_ram_is_shared_and_only_sent_when_supported(self):
        camera, tab = self.make_tab()
        operation_card = BurnerCameraCard(camera)
        self.addCleanup(operation_card.shutdown)
        camera.publish(selected='usb:1',
                       capabilities=['CaptureInRam', 'CaptureNoAf'],
                       capture_in_ram=True)
        self.assertTrue(tab.capture_in_ram.isEnabled())
        self.assertTrue(tab.capture_in_ram.isChecked())
        camera.calls.clear()
        tab.capture_button.click()
        tab.capture_no_af_button.click()
        operation_card.capture_button.click()
        self.assertEqual(camera.calls, [
            ('action', 'capture', {'capture_in_ram': True}),
            ('action', 'capture_no_af', {'capture_in_ram': True}),
            ('action', 'capture', {'capture_in_ram': True}),
        ])

        camera.calls.clear()
        tab.capture_in_ram.setChecked(False)
        self.assertEqual(camera.calls,
                         [('action', 'capture_target', {'capture_in_ram': False})])
        camera.publish(capture_in_ram=False)
        operation_card.capture_button.click()
        self.assertEqual(camera.calls[-1],
                         ('action', 'capture', {'capture_in_ram': False}))

        camera.publish(capabilities=[], capture_in_ram=False)
        self.assertFalse(tab.capture_in_ram.isEnabled())
        camera.calls.clear()
        tab.capture_button.click()
        operation_card.capture_button.click()
        self.assertEqual(camera.calls, [
            ('action', 'capture', {}), ('action', 'capture', {}),
        ])

    def test_stop_controls_remain_available_while_camera_is_busy(self):
        camera, tab = self.make_tab()
        camera.publish(selected='usb:1', busy=True,
                       capabilities=['LiveView', 'RecordMovie', 'Bulb'])
        self.assertFalse(tab.live_start_button.isEnabled())
        self.assertFalse(tab.video_start_button.isEnabled())
        self.assertFalse(tab.bulb_start_button.isEnabled())
        self.assertTrue(tab.live_stop_button.isEnabled())
        self.assertTrue(tab.video_stop_button.isEnabled())
        self.assertTrue(tab.bulb_stop_button.isEnabled())
        camera.calls.clear()
        tab.live_stop_button.click()
        tab.video_stop_button.click()
        tab.bulb_stop_button.click()
        self.assertEqual(camera.calls, [
            ('stop_preview',),
            ('action', 'video_stop', {}),
            ('action', 'bulb_stop', {}),
        ])

        camera.busy_changed.emit(True)
        self.assertTrue(tab.live_stop_button.isEnabled())
        self.assertFalse(tab.video_stop_button.isEnabled())
        self.assertFalse(tab.bulb_stop_button.isEnabled())
        camera.busy_changed.emit(False)
        camera.publish(workflow='timelapse')
        self.assertTrue(tab.live_stop_button.isEnabled())
        self.assertFalse(tab.video_stop_button.isEnabled())
        self.assertFalse(tab.bulb_stop_button.isEnabled())

    def test_operation_tab_camera_optional_and_log_path_bounded(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        plain = OperationTab(session)
        self.addCleanup(plain.close)
        self.assertFalse(hasattr(plain, 'camera_card'))
        camera = FakeCamera()
        camera_tab = OperationTab(session, camera=camera)
        self.addCleanup(camera_tab.close)
        self.addCleanup(camera_tab.camera_card.shutdown)
        self.assertIs(camera_tab.camera_card.camera, camera)
        self.assertEqual(camera_tab.log_path.maximumWidth(), theme.scale(280))
        long_path = 'C:/a/very/long/capture/session/output/location/run.csv'
        camera_tab.log_path.setText(long_path)
        self.assertEqual(camera_tab.log_path.toolTip(), long_path)

    def test_main_window_rebuild_retains_camera_and_tab(self):
        from flow_controller.ui import qt_main_window

        camera = FakeCamera()
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        with patch.object(qt_main_window, 'DirectCamera', return_value=camera):
            window = qt_main_window.MainWindow(session)
            old_tab = window.camera_tab
            window._rebuild()
            self.assertIs(window.camera, camera)
            self.assertIs(window.camera_tab, old_tab)
            self.assertIs(window.operation_tab.camera_card.camera, camera)
            window.close()
            self.app.processEvents()
            self.assertIn(('shutdown',), camera.calls)


if __name__ == '__main__':
    unittest.main()
