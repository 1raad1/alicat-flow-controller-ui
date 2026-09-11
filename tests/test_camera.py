"""USB scheduler tests without camera hardware, HTTP, or a CLR dependency."""
import copy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from flow_controller.core.camera import DirectCamera, _new_workflow


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.threads = set()
        self.events = []
        self.opened = self.closed = False
        self.delay = 0
        self.fail_action = ''
        self.defer_capture = False
        self.frames = 0
        self.frame_gate = None
        self.timeline = []
        self.state = dict(cameras=[{'id': '123', 'name': 'USB test'}], selected='123',
            capabilities=['LiveView', 'CaptureNoAf'], battery=70, busy=False,
            properties=[dict(name='IsoNumber', label='ISO', value='100',
                             values=['100', '200', '400'], readonly=False)])
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(0xFFAA3300)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'JPEG')
        self.jpeg = bytes(data)

    def open(self):
        self.threads.add(threading.get_ident())
        self.opened = True
        return self.snapshot()

    def scan(self):
        return self.snapshot()

    def select(self, camera_id):
        self.state['selected'] = camera_id
        return self.snapshot()

    def snapshot(self):
        self.threads.add(threading.get_ident())
        return copy.deepcopy(self.state)

    def pump(self):
        pass

    def execute(self, name, params):
        self.threads.add(threading.get_ident())
        self.calls.append((name, params))
        self.timeline.append(name)
        if name == self.fail_action:
            raise RuntimeError('USB action failed')
        if self.delay and name == 'capture':
            time.sleep(self.delay)
        if name == 'set_property':
            self.state['properties'][0]['value'] = params['value']
        if name in ('capture', 'capture_no_af'):
            if self.defer_capture:
                self.state['busy'] = True
            else:
                self.events.append({'type': 'captured', 'path': 'test.jpg'})
        return True

    def frame(self):
        self.frames += 1
        self.timeline.append('frame')
        if self.frame_gate:
            self.frame_gate.wait(2)
        return self.jpeg

    def poll_events(self):
        events, self.events = self.events, []
        self.timeline.extend('event:' + event['type'] for event in events)
        return events

    def complete_capture(self, *, event_type='capture_completed'):
        self.state['busy'] = False
        event = {'type': event_type}
        if event_type == 'captured':
            event['path'] = 'test.jpg'
        self.events.append(event)

    def close(self):
        self.closed = True


class CameraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.engine = FakeEngine()
        self.camera = DirectCamera(engine_factory=lambda: self.engine)
        self.errors = []
        self.camera.error.connect(self.errors.append)

    def test_output_change_applies_without_an_app_capture_command(self):
        self.camera.connect_camera()
        self.assertTrue(self.wait_for(lambda: not self.camera.busy))
        with tempfile.TemporaryDirectory() as destination:
            self.camera.set_output_directory(destination)
            self.assertTrue(self.wait_for(lambda: any(
                name == 'set_output' and Path(params['path']) == Path(destination)
                for name, params in self.engine.calls)))
        self.assertFalse(any(name == 'capture' for name, _ in self.engine.calls))

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(.005)
        return bool(predicate())

    def tearDown(self):
        if self.engine.frame_gate:
            self.engine.frame_gate.set()
        self.assertTrue(self.wait_for(self.camera.shutdown))
        self.camera.deleteLater()
        self.app.processEvents()

    def connect(self):
        self.assertTrue(self.camera.connect_camera())
        self.assertTrue(self.wait_for(lambda: not self.camera.busy))
        self.assertEqual(self.camera.state['selected'], '123')

    def test_idle_does_not_start_a_thread_or_open_usb(self):
        self.assertIsNone(self.camera._thread)
        self.assertFalse(self.engine.opened)
        self.assertFalse(self.camera._timer.isActive())

    def test_all_camera_calls_run_on_one_non_ui_thread(self):
        self.connect()
        self.camera.action('capture')
        self.assertTrue(self.wait_for(lambda: not self.camera.busy))
        self.assertEqual(len(self.engine.threads), 1)
        self.assertNotIn(threading.get_ident(), self.engine.threads)

    def test_slow_capture_does_not_block_ui_or_accept_duplicate(self):
        self.connect()
        self.engine.delay = .2
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        self.camera.action('capture')
        self.assertFalse(self.camera.action('capture'))
        self.assertTrue(self.wait_for(lambda: not self.camera.busy))
        timer.stop()
        self.assertGreater(len(ticks), 8)
        self.assertEqual(sum(n == 'capture' for n, _ in self.engine.calls), 1)

    def test_disconnect_releases_worker_and_reconnect_restarts_it(self):
        self.connect()
        first = self.camera._thread
        self.camera.action('disconnect')
        self.assertTrue(self.wait_for(lambda: not first.is_alive() and not self.camera.busy))
        self.assertTrue(self.engine.closed)
        self.assertFalse(self.camera._timer.isActive())
        self.connect()
        self.assertIsNot(self.camera._thread, first)

    def test_preview_shares_one_decoded_frame_and_drops_backlog(self):
        self.connect()
        frames_a, frames_b = [], []
        self.camera.frame_received.connect(frames_a.append)
        self.camera.frame_received.connect(frames_b.append)
        self.camera.set_fps(30)
        self.camera.start_preview()
        time.sleep(.25)  # Deliberately do not drain Qt while worker reads frames.
        self.assertGreaterEqual(self.engine.frames, 3)
        self.camera._drain()
        self.assertEqual(len(frames_a), 1)
        self.assertEqual(frames_a[0].cacheKey(), frames_b[0].cacheKey())
        self.assertIsNone(self.camera._control.image)

    def test_stop_discards_an_in_flight_frame_and_stops_device(self):
        self.connect()
        self.engine.frame_gate = threading.Event()
        frames = []
        self.camera.frame_received.connect(frames.append)
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: self.engine.frames > 0))
        self.camera.stop_preview()
        self.engine.frame_gate.set()
        self.assertTrue(self.wait_for(lambda: any(n == 'live_stop' for n, _ in self.engine.calls)))
        self.camera._drain()
        self.assertTrue(all(f.isNull() for f in frames))

    def test_missing_capability_and_invalid_image_are_reported(self):
        self.engine.state['capabilities'] = []
        self.connect()
        self.assertFalse(self.camera.start_preview())
        self.assertIn('does not support', self.errors[-1])
        self.camera.state['capabilities'] = ['LiveView']
        self.engine.jpeg = b'invalid'
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: any('invalid preview' in e for e in self.errors)))

    def test_photo_events_and_output_directory(self):
        self.connect()
        saved = []
        self.camera.captured.connect(saved.append)
        with tempfile.TemporaryDirectory() as folder:
            self.camera.set_output_directory(folder)
            self.camera.action('capture')
            self.assertTrue(self.wait_for(lambda: bool(saved)))
            self.assertIn(('set_output', {'path': str(Path(folder).absolute())}), self.engine.calls)

    def test_live_view_stops_for_capture_and_restarts_after_completion(self):
        self.connect()
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: self.camera.previewing))
        self.engine.timeline.clear()
        saved = []
        self.camera.captured.connect(saved.append)

        self.camera.action('capture')

        self.assertTrue(self.wait_for(lambda: bool(saved) and self.camera.previewing))
        sequence = [item for item in self.engine.timeline
                    if item in ('live_stop', 'capture', 'event:captured', 'live_start')]
        self.assertEqual(sequence,
                         ['live_stop', 'capture', 'event:captured', 'live_start'])

    def test_capture_wait_blocks_frames_and_restart_until_camera_is_idle(self):
        self.connect()
        self.engine.defer_capture = True
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: self.camera.previewing and
                                     self.engine.frames > 0))
        self.camera.action('capture')
        self.assertTrue(self.wait_for(lambda: any(
            name == 'capture' for name, _params in self.engine.calls)))
        self.assertTrue(self.wait_for(lambda: not self.camera.previewing))
        frames_after_stop = self.engine.frames
        starts_after_stop = sum(name == 'live_start'
                                for name, _params in self.engine.calls)

        self.engine.events.append({'type': 'captured', 'path': 'test.jpg'})
        self.engine.state['busy'] = True
        self.engine.timeline.append('transfer-ready')
        time.sleep(.08)
        self.app.processEvents()
        self.assertEqual(self.engine.frames, frames_after_stop)
        self.assertEqual(sum(name == 'live_start'
                             for name, _params in self.engine.calls),
                         starts_after_stop)

        self.engine.complete_capture()
        self.assertTrue(self.wait_for(lambda: self.camera.previewing))
        self.assertGreater(sum(name == 'live_start'
                               for name, _params in self.engine.calls),
                           starts_after_stop)

    def test_user_stop_during_capture_wait_prevents_preview_restart(self):
        self.connect()
        self.engine.defer_capture = True
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: self.camera.previewing))
        self.camera.action('capture')
        self.assertTrue(self.wait_for(lambda: any(
            name == 'capture' for name, _params in self.engine.calls)))
        self.camera.stop_preview()
        starts = sum(name == 'live_start' for name, _params in self.engine.calls)

        self.engine.complete_capture()
        self.assertTrue(self.wait_for(
            lambda: 'event:capture_completed' in self.engine.timeline))
        time.sleep(.08)
        self.app.processEvents()
        self.assertFalse(self.camera.previewing)
        self.assertEqual(sum(name == 'live_start'
                             for name, _params in self.engine.calls), starts)

    def test_capture_event_error_stops_automatic_preview_restart(self):
        self.connect()
        self.engine.defer_capture = True
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: self.camera.previewing))
        self.camera.action('capture')
        self.assertTrue(self.wait_for(lambda: any(
            name == 'capture' for name, _params in self.engine.calls)))
        starts = sum(name == 'live_start' for name, _params in self.engine.calls)

        self.engine.state['busy'] = False
        self.engine.events.append({'type': 'error', 'message': 'transfer failed'})
        self.assertTrue(self.wait_for(lambda: 'transfer failed' in self.errors))
        time.sleep(.08)
        self.app.processEvents()
        self.assertFalse(self.camera.previewing)
        self.assertFalse(self.camera._control.desired_preview)
        self.assertEqual(sum(name == 'live_start'
                             for name, _params in self.engine.calls), starts)

    def test_capture_command_failure_releases_wait_without_auto_restart(self):
        self.connect()
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: self.camera.previewing))
        starts = sum(name == 'live_start' for name, _params in self.engine.calls)
        self.engine.fail_action = 'capture'

        self.camera.action('capture')

        self.assertTrue(self.wait_for(lambda: 'USB action failed' in self.errors))
        time.sleep(.08)
        self.app.processEvents()
        self.assertFalse(self.camera.previewing)
        self.assertFalse(self.camera._control.desired_preview)
        self.assertEqual(sum(name == 'live_start'
                             for name, _params in self.engine.calls), starts)

    def test_timelapse_waits_for_capture_completion_and_stops_at_count(self):
        self.connect()
        self.camera.action('timelapse_start', count=2, interval=.5, autofocus=True)
        self.assertTrue(self.wait_for(lambda: sum(n == 'capture' for n, _ in self.engine.calls) == 2))
        self.assertTrue(self.wait_for(lambda: not self.camera.state['workflow']))
        self.assertFalse(self.errors)

    def test_bracket_restores_original_value_after_completion_and_error(self):
        self.connect()
        self.camera.action('bracket_start', property='IsoNumber', values=['200'], autofocus=True)
        self.assertTrue(self.wait_for(lambda: any(n == 'capture' for n, _ in self.engine.calls)))
        self.assertTrue(self.wait_for(lambda: self.engine.state['properties'][0]['value'] == '100'))
        self.assertTrue(self.wait_for(lambda: not self.camera.state['workflow'] and not self.camera.busy))
        self.assertEqual(self.engine.state['properties'][0]['value'], '100')
        self.engine.fail_action = 'capture'
        self.camera.action('bracket_start', property='IsoNumber', values=['400'], autofocus=True)
        self.assertTrue(self.wait_for(lambda: bool(self.errors)))
        self.assertEqual(self.engine.state['properties'][0]['value'], '100')

    def test_shutdown_is_nonblocking_and_closes_engine_even_if_live_stop_fails(self):
        self.connect()
        self.camera.start_preview()
        self.assertTrue(self.wait_for(lambda: self.engine.frames > 0))
        self.engine.fail_action = 'live_stop'
        started = time.monotonic()
        self.camera.shutdown()
        self.assertLess(time.monotonic() - started, .05)
        self.assertTrue(self.wait_for(self.camera.shutdown))
        self.assertTrue(self.engine.closed)

    def test_workflow_parameters_are_validated_before_driver_calls(self):
        for params in ({'interval': float('nan')}, {'count': 0}, {'interval': .1}):
            with self.assertRaises(ValueError):
                _new_workflow('timelapse_start', params, self.engine.state)
        with self.assertRaises(ValueError):
            _new_workflow('bracket_start', {'property': 'IsoNumber', 'values': ['bad']}, self.engine.state)

    def test_real_facade_updates_property_controls_and_preserves_error_status(self):
        from unittest.mock import patch
        from PySide6.QtCore import QSettings
        from flow_controller.ui.qt_camera import CameraTab
        with tempfile.TemporaryDirectory() as folder:
            settings = QSettings(str(Path(folder) / 'camera.ini'), QSettings.Format.IniFormat)
            with patch('flow_controller.ui.qt_camera.QSettings', return_value=settings):
                tab = CameraTab(self.camera)
            self.connect()
            self.assertTrue(tab.properties_table.cellWidget(0, 3).isEnabled())
            tab._property_editors['IsoNumber'].setCurrentText('200')
            tab.properties_table.cellWidget(0, 3).click()
            self.assertTrue(self.wait_for(lambda: not self.camera.busy))
            self.assertEqual(self.engine.state['properties'][0]['value'], '200')
            self.assertTrue(tab.properties_table.cellWidget(0, 3).isEnabled())
            self.engine.fail_action = 'capture'
            self.camera.action('capture')
            self.assertTrue(self.wait_for(lambda: not self.camera.busy))
            self.assertIn('USB action failed', tab.connection_status.text())
            tab.shutdown()
            tab.deleteLater()


if __name__ == '__main__':
    unittest.main()
