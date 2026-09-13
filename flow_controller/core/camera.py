"""Direct USB camera control: one STA worker, one replaceable decoded frame.

No desktop process, sockets, polling HTTP, or duplicate image decoders. Driver
calls never run on Qt's UI thread or the flow-acquisition worker.
"""
from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import queue
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage


def empty_state():
    return dict(cameras=[], selected='', capabilities=[], properties=[],
                battery=None, workflow='', recording=False,
                bulb_active=False)


def _engine_factory():
    from ..infrastructure.digicam_engine import DccEngine
    return DccEngine()


class _Control:
    def __init__(self):
        self.commands = queue.Queue(maxsize=8)
        self.events = queue.Queue()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.cancel_workflow = threading.Event()
        self.desired_preview = False
        self.generation = 0
        self.fps = 10
        self.output = str(Path.home() / 'Pictures' / 'Flow Controller')
        self.image_lock = threading.Lock()
        self.image = None

    def emit(self, kind, value):
        self.events.put((kind, value))

    def clear_frame(self):
        with self.image_lock:
            self.image = None


class DirectCamera(QObject):
    frame_received = Signal(QImage)
    status_changed = Signal(str)
    error = Signal(str)
    busy_changed = Signal(bool)
    previewing_changed = Signal(bool)
    state_changed = Signal(dict)
    captured = Signal(str)
    action_finished = Signal(str, object)

    def __init__(self, parent=None, *, engine_factory=None):
        super().__init__(parent)
        self.state = empty_state()
        self.busy = False
        self.previewing = False
        self._factory = engine_factory or _engine_factory
        self._control = _Control()
        self._thread = None
        self._closing = False
        self._busy_since = None
        self._slow_reported = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._drain)

    def connect_camera(self):
        return self.action('connect')

    def select_camera(self, camera_id):
        self.stop_preview()
        return self.action('select', id=str(camera_id))

    def set_output_directory(self, path):
        value = str(path).strip()
        if not value:
            raise ValueError('Choose a photo output folder.')
        self._control.output = str(Path(value).expanduser().absolute())

    def set_fps(self, fps):
        fps = int(fps)
        if not 1 <= fps <= 30:
            raise ValueError('Preview rate must be between 1 and 30 FPS.')
        self._control.fps = fps

    def action(self, action_name, **params):
        name = action_name
        if self._closing:
            self.error.emit('Camera is closing.')
            return False
        if name == 'workflow_stop':
            self._control.cancel_workflow.set()
            self._control.wake.set()
            return True
        if self.busy or (self.state.get('workflow') and name != 'disconnect'):
            self.error.emit('Wait for the camera action, or stop the capture sequence first.')
            return False
        if name != 'connect' and (self._thread is None or not self._thread.is_alive()):
            self.error.emit('Discover and select a USB camera first.')
            return False
        if name == 'disconnect':
            self.stop_preview()
            self._control.cancel_workflow.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=_camera_worker,
                args=(self._control, self._factory), name='USB camera', daemon=True)
            self._thread.start()
            self._timer.start()
        self._control.commands.put_nowait((name, params))
        self._set_busy(True)
        self._control.wake.set()
        return True

    def start_preview(self):
        if self._closing or not self.state.get('selected'):
            self.error.emit('Discover and select a USB camera first.')
            return False
        if 'LiveView' not in self.state.get('capabilities', []):
            self.error.emit('This camera does not support live view.')
            return False
        self._control.desired_preview = True
        self._control.wake.set()
        return True

    def stop_preview(self):
        self._control.desired_preview = False
        self._control.generation += 1
        self._control.clear_frame()
        self._set_previewing(False)
        self.frame_received.emit(QImage())
        self.status_changed.emit('Live view stopped')
        self._control.wake.set()

    def shutdown(self):
        """Request cleanup, without waiting on a USB driver in the Qt thread."""
        self._closing = True
        self.stop_preview()
        self._control.stop.set()
        self._control.wake.set()
        if self._thread is not None and self._thread.is_alive():
            return False
        self._drain()
        self._timer.stop()
        return True

    def _set_busy(self, busy):
        if self.busy != busy:
            self.busy = busy
            self._busy_since = time.monotonic() if busy else None
            self._slow_reported = False
            self.busy_changed.emit(busy)

    def _set_previewing(self, previewing):
        previewing = bool(previewing)
        if self.previewing != previewing:
            self.previewing = previewing
            self.previewing_changed.emit(previewing)

    def _drain(self):
        for _ in range(100):
            try:
                kind, value = self._control.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'state':
                self.state = value
                self.state_changed.emit(value)
            elif kind == 'error':
                self.error.emit(value)
                self.status_changed.emit(value)
            elif kind == 'status':
                self.status_changed.emit(value)
            elif kind == 'captured':
                self.captured.emit(value)
            elif kind == 'finished':
                self._set_busy(False)
                self.action_finished.emit(*value)
            elif kind == 'failed':
                self._set_busy(False)
            elif kind == 'preview':
                generation, active = value
                if generation == self._control.generation:
                    self._set_previewing(active)
                    if not active:
                        self.frame_received.emit(QImage())
            elif kind == 'clear':
                self.frame_received.emit(QImage())
        with self._control.image_lock:
            image, self._control.image = self._control.image, None
        if image is not None and self._control.desired_preview:
            generation, decoded = image
            if generation == self._control.generation:
                self.frame_received.emit(decoded)
        if (self.busy and self._busy_since and not self._slow_reported
                and time.monotonic() - self._busy_since > 35):
            self._slow_reported = True
            self.status_changed.emit('Camera driver is still busy; flow control remains available.')
        if self._thread is not None and not self._thread.is_alive():
            self._timer.stop()


def _camera_worker(control, factory):
    engine = None
    live = False
    last_output = None
    workflow = None
    capture_waiting = False
    next_frame = last_good = last_error = 0.0
    initialised_com = False

    def snapshot():
        state = engine.snapshot() if engine is not None else empty_state()
        state['workflow'] = workflow['kind'] if workflow else ''
        control.emit('state', state)
        return state

    def end_workflow():
        nonlocal workflow
        old, workflow = workflow, None
        if old and old.get('restore') is not None and engine is not None:
            try:
                engine.execute('set_property', {'name': old['property'], 'value': old['restore']})
            except Exception as exc:
                control.emit('error', f'Could not restore bracket setting: {exc}')
        control.cancel_workflow.clear()
        snapshot()

    def stop_live():
        nonlocal live
        if live and engine is not None:
            try:
                engine.execute('live_stop', {})
            finally:
                live = False
                control.clear_frame()
                control.emit('preview', (control.generation, False))
                control.emit('status', 'Live view stopped')
            return
        live = False
        control.clear_frame()
        control.emit('preview', (control.generation, False))
        control.emit('status', 'Live view stopped')

    def trigger_capture(name, params):
        nonlocal live, capture_waiting
        # Canon has a dedicated live-view shutter path. Suspend frame reads,
        # but leave that mode active when the selected image format supports it.
        keep_live = live and engine.snapshot().get('capture_preserves_live_view', False)
        params = dict(params)
        if keep_live:
            params['live_view_capture'] = True
        if live and not keep_live:
            engine.execute('live_stop', {})
            live = False
            control.emit('preview', (control.generation, False))
        capture_waiting = True
        control.emit('status', 'Capturing photo…')
        try:
            return engine.execute(name, params)
        except Exception:
            capture_waiting = False
            control.desired_preview = False
            raise

    try:
        if os.name == 'nt':
            result = ctypes.windll.ole32.CoInitializeEx(None, 2)
            if result not in (0, 1):
                raise RuntimeError(f'Could not initialise the USB camera thread (COM {result}).')
            initialised_com = True
        while not control.stop.is_set():
            if control.cancel_workflow.is_set():
                end_workflow()
                control.emit('status', 'Capture sequence stopped')
            try:
                name, params = control.commands.get_nowait()
            except queue.Empty:
                name = None
            if name is not None:
                result = None
                succeeded = False
                try:
                    if name == 'connect':
                        if engine is None:
                            engine = factory()
                            engine.open()
                            last_output = None
                        else:
                            engine.scan()
                        result = snapshot()
                        control.emit('status', f"{len(result['cameras'])} USB camera(s) found")
                    elif name == 'disconnect':
                        end_workflow()
                        stop_live()
                        engine.close()
                        engine = None
                        capture_waiting = False
                        result = snapshot()
                        control.emit('status', 'USB camera disconnected')
                    elif engine is None:
                        raise RuntimeError('Discover a USB camera first.')
                    elif name == 'select':
                        stop_live()
                        engine.select(params['id'])
                        result = snapshot()
                    elif name == 'refresh':
                        result = snapshot()
                    elif name in ('timelapse_start', 'bracket_start'):
                        state = engine.snapshot()
                        if not state.get('selected'):
                            raise ValueError('Select a camera before starting a capture sequence.')
                        workflow = _new_workflow(name, params, state)
                        result = snapshot()
                    else:
                        if last_output != control.output:
                            engine.execute('set_output', {'path': control.output})
                            last_output = control.output
                        if name in ('capture', 'capture_no_af'):
                            result = trigger_capture(name, params)
                        else:
                            result = engine.execute(name, params)
                        snapshot()
                    succeeded = True
                except Exception as exc:
                    control.emit('error', str(exc))
                    if name == 'connect' and engine is not None:
                        try:
                            engine.close()
                        except Exception:
                            pass
                        engine = None
                        snapshot()
                finally:
                    control.emit('finished' if succeeded else 'failed', (name, result))
                if engine is None and name in ('connect', 'disconnect'):
                    break
            if engine is not None:
                try:
                    # Physical shutter presses also produce transfer events, so
                    # apply the destination before draining those events.
                    if last_output != control.output:
                        engine.execute('set_output', {'path': control.output})
                        last_output = control.output
                    engine.pump()
                    capture_changed = False
                    for event in engine.poll_events():
                        if event['type'] == 'captured':
                            control.emit('captured', event['path'])
                            control.emit('status', f"Photo saved: {event['path']}")
                            capture_changed = True
                            if workflow and workflow.get('waiting'):
                                workflow['received'] = True
                        elif event['type'] == 'capture_completed':
                            capture_changed = True
                            if workflow and workflow.get('waiting'):
                                workflow['received'] = True
                        elif event['type'] == 'error':
                            control.emit('error', event['message'])
                            capture_changed = True
                            if capture_waiting:
                                capture_waiting = False
                                control.desired_preview = False
                            if workflow:
                                end_workflow()
                        elif event['type'] == 'connection':
                            state = snapshot()
                            if not state.get('selected'):
                                capture_waiting = False
                                control.desired_preview = False
                                control.generation += 1
                                live = False
                                control.clear_frame()
                                control.emit('preview', (control.generation, False))
                                if workflow:
                                    end_workflow()
                    if capture_changed:
                        state = snapshot()
                        if not state.get('busy', False):
                            if capture_waiting:
                                last_good = time.monotonic()
                            capture_waiting = False
                    if control.desired_preview and not live and not capture_waiting:
                        generation = control.generation
                        engine.execute('live_start', {})
                        live = True
                        last_good = time.monotonic()
                        control.emit('preview', (generation, True))
                        if control.desired_preview and generation == control.generation:
                            control.emit('status', 'Live view started')
                    if not control.desired_preview and live:
                        stop_live()
                    now = time.monotonic()
                    if workflow and workflow.get('waiting'):
                        if workflow.get('received') and not engine.snapshot().get('busy', False):
                            workflow['waiting'] = False
                            if workflow['done'] >= workflow['count']:
                                end_workflow()
                            else:
                                workflow['next'] = now + workflow['interval']
                        elif now - workflow['triggered_at'] > 60:
                            raise RuntimeError('Capture sequence stopped: camera did not complete the shot within 60 seconds.')
                    if workflow and not workflow.get('waiting') and now >= workflow['next']:
                        if last_output != control.output:
                            engine.execute('set_output', {'path': control.output})
                            last_output = control.output
                        if workflow['kind'] == 'bracket':
                            engine.execute('set_property', {
                                'name': workflow['property'],
                                'value': workflow['values'][workflow['done']]})
                        trigger_capture('capture' if workflow['autofocus'] else 'capture_no_af',
                                        {'autofocus_before_capture': workflow['autofocus']})
                        workflow['done'] += 1
                        workflow['waiting'] = True
                        workflow['received'] = False
                        workflow['triggered_at'] = time.monotonic()
                        control.emit('status', f"Capture sequence: {workflow['done']}/{workflow['count']} shots triggered")
                    if live and control.desired_preview and not capture_waiting and now >= next_frame:
                        generation = control.generation
                        data = engine.frame()
                        if data:
                            if len(data) > 12 * 1024 * 1024:
                                raise ValueError('Camera preview exceeded the 12 MB frame limit.')
                            image = QImage.fromData(data)
                            if image.isNull():
                                raise ValueError('Camera returned an invalid preview image.')
                            with control.image_lock:
                                control.image = (generation, image)
                            last_good = time.monotonic()
                        elif now - last_good > 3:
                            control.clear_frame()
                            control.emit('clear', None)
                            if now - last_error > 3:
                                last_error = now
                                control.emit('status', 'No live frame received for 3 seconds')
                        next_frame = time.monotonic() + 1.0 / control.fps
                except Exception as exc:
                    if workflow:
                        end_workflow()
                    control.clear_frame()
                    control.emit('clear', None)
                    now = time.monotonic()
                    if now - last_error > 2:
                        last_error = now
                        control.emit('error', str(exc))
                    if control.desired_preview and not live:
                        control.desired_preview = False
                    next_frame = now + 1
            control.wake.wait(0.02 if engine is not None else 0.2)
            control.wake.clear()
    except Exception as exc:
        control.emit('error', str(exc))
        control.emit('failed', ('connect', None))
    finally:
        if engine is not None:
            for cleanup in (end_workflow, stop_live, engine.close):
                try:
                    cleanup()
                except Exception as exc:
                    control.emit('error', f'Camera cleanup failed: {exc}')
        control.emit('state', empty_state())
        if initialised_com:
            ctypes.windll.ole32.CoUninitialize()


def _new_workflow(name, params, state):
    autofocus = bool(params.get('autofocus', True))
    if not autofocus and 'CaptureNoAf' not in state.get('capabilities', []):
        raise ValueError('This camera does not support capture without autofocus.')
    if name == 'timelapse_start':
        interval = float(params.get('interval', 5))
        count = int(params.get('count', 10))
        if not math.isfinite(interval) or interval < 0.5 or not 1 <= count <= 10000:
            raise ValueError('Use at least 0.5 seconds between shots and 1–10000 shots.')
        return dict(kind='timelapse', count=count, interval=interval, done=0,
                    next=time.monotonic(), autofocus=autofocus)
    prop = next((p for p in state['properties'] if p['name'] == params.get('property')), None)
    values = list(params.get('values', []))
    if (not prop or prop.get('readonly') or not 1 <= len(values) <= 100
            or any(v not in prop['values'] for v in values)):
        raise ValueError('Choose a writable camera setting and supported bracket values.')
    return dict(kind='bracket', count=len(values), values=values,
                property=prop['name'], restore=prop['value'], interval=1,
                done=0, next=time.monotonic(), autofocus=autofocus)
