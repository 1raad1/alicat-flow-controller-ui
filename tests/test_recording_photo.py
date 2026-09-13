"""Hardware-free checks for photos attached to data recordings."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication

from flow_controller.core.recording_photo import RecordingPhoto
from flow_controller.core.session import FlowSession
from flow_controller.ui.qt_operation_tab import OperationTab
from flow_controller.ui.qt_widgets import Card


class FakeCamera(QObject):
    captured = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.state = {
            'selected': 'usb:camera',
            'capabilities': ['CaptureInRam'],
            'capture_in_ram': True,
            'workflow': '',
            'recording': False,
            'bulb_active': False,
        }
        self.busy = False
        self.accept_actions = True
        self.calls = []

    def action(self, name, **parameters):
        self.calls.append((name, parameters))
        return self.accept_actions


class RecordingPhotoTests(unittest.TestCase):
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
        QSettings('FlowController', 'Camera').clear()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        preferences = patch.dict(os.environ, {
            'FLOW_CONTROLLER_UNIT_PREFS': str(Path(self.root.name) / 'units.json'),
            'FLOW_CONTROLLER_COMBUSTION_PREFS': str(
                Path(self.root.name) / 'combustion.json'),
        })
        preferences.start()
        self.addCleanup(preferences.stop)

    def make_session(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        session.log_dir = Path(self.root.name)
        session.log_destination = str(Path(self.root.name) / 'labview.csv')
        messages = []
        session.logged.connect(lambda _channel, text: messages.append(text))
        self.addCleanup(session.shutdown)
        return session, messages

    def start(self, session, name='run.csv'):
        path = Path(self.root.name) / name
        self.assertTrue(session.start_logging(path))
        self.assertTrue(path.exists())
        return path

    def test_default_is_off_and_failed_log_does_not_capture(self):
        session, _messages = self.make_session()
        camera = FakeCamera()
        photo = RecordingPhoto(session, camera)

        self.assertFalse(photo.enabled)
        self.start(session)
        self.assertEqual(camera.calls, [])
        session.stop_logging()

        photo.set_enabled(True)
        with patch.object(session._csv, 'start', side_effect=OSError('denied')):
            self.assertFalse(session.start_logging(Path(self.root.name) / 'failed.csv'))
        self.assertEqual(camera.calls, [])

    def test_manual_start_requests_exactly_one_photo_and_forwards_ram_target(self):
        session, messages = self.make_session()
        camera = FakeCamera()
        photo = RecordingPhoto(session, camera)
        photo.set_enabled(True)

        path = self.start(session)
        self.assertEqual(camera.calls, [('capture', {'capture_in_ram': True})])
        self.assertFalse(session.start_logging(Path(self.root.name) / 'again.csv'))
        session.stop_logging()
        self.assertEqual(camera.calls, [('capture', {'capture_in_ram': True})])

        camera.captured.emit('C:/photos/burner.jpg')
        self.assertTrue(any(f'Burner photo for {path} saved:' in line
                            for line in messages))
        self.assertIsNone(photo._pending_log)

    def test_labview_log_uses_the_same_capture_path(self):
        session, messages = self.make_session()
        camera = FakeCamera()
        photo = RecordingPhoto(session, camera)
        photo.set_enabled(True)

        session._on_udp_command('log')

        self.assertTrue(session.logging_active)
        self.assertEqual(camera.calls, [('capture', {'capture_in_ram': True})])
        self.assertTrue(any('Logging started (LabVIEW)' in line for line in messages))
        self.assertTrue(any('Burner photo requested for' in line for line in messages))

    def test_disconnected_and_busy_cameras_are_skipped_without_stopping_logging(self):
        for name, changes, reason in (
                ('disconnected', {'selected': ''}, 'connect and select a camera'),
                ('busy', {'busy': True}, 'camera is busy')):
            with self.subTest(name=name):
                session, messages = self.make_session()
                camera = FakeCamera()
                for key, value in changes.items():
                    if key == 'busy':
                        camera.busy = value
                    else:
                        camera.state[key] = value
                photo = RecordingPhoto(session, camera)
                photo.set_enabled(True)

                self.start(session, f'{name}.csv')

                self.assertTrue(session.logging_active)
                self.assertEqual(camera.calls, [])
                self.assertTrue(any('Burner photo skipped' in line and reason in line
                                    and 'Data logging continues.' in line
                                    for line in messages))
                session.stop_logging()

    def test_rejection_and_transfer_error_are_reported_and_cleared(self):
        for name, reject, error, expected in (
                ('rejected', True, None, 'did not accept the capture request'),
                ('error', False, 'USB transfer failed', 'USB transfer failed')):
            with self.subTest(name=name):
                session, messages = self.make_session()
                camera = FakeCamera()
                camera.accept_actions = not reject
                photo = RecordingPhoto(session, camera)
                photo.set_enabled(True)
                self.start(session, f'{name}.csv')
                if error:
                    camera.error.emit(error)

                self.assertIsNone(photo._pending_log)
                self.assertTrue(any('Burner photo failed' in line and expected in line
                                    and 'Data logging continues.' in line
                                    for line in messages))
                self.assertTrue(session.logging_active)
                session.stop_logging()

    def test_timeout_is_reported_and_allows_the_next_recording(self):
        session, messages = self.make_session()
        camera = FakeCamera()
        photo = RecordingPhoto(session, camera)
        photo.set_enabled(True)
        self.start(session)

        photo._timeout.timeout.emit()

        self.assertIsNone(photo._pending_log)
        self.assertFalse(photo._timeout.isActive())
        self.assertTrue(any('No photo transfer was confirmed within 60 seconds.' in line
                            and 'Data logging continues.' in line
                            for line in messages))
        session.stop_logging()
        self.start(session, 'next.csv')
        self.assertEqual(len(camera.calls), 2)

    def test_toggle_persists_and_rebuilding_operation_tab_does_not_duplicate_capture(self):
        session, _messages = self.make_session()
        camera = FakeCamera()
        photo = RecordingPhoto(session, camera)
        session.recording_photo = photo

        with patch('flow_controller.ui.qt_camera.BurnerCameraCard',
                   side_effect=lambda _camera: Card('DSLR Camera')):
            first = OperationTab(session, camera=camera)
            self.addCleanup(first.close)
            first.recording_photo_toggle.setChecked(True)
            self.assertTrue(photo.enabled)
            self.assertTrue(QSettings('FlowController', 'Camera').value(
                'photo_on_recording', False, type=bool))

            second = OperationTab(session, camera=camera)
            self.addCleanup(second.close)
            self.assertTrue(second.recording_photo_toggle.isChecked())

        self.start(session)
        self.assertEqual(camera.calls, [('capture', {'capture_in_ram': True})])


if __name__ == '__main__':
    unittest.main()
