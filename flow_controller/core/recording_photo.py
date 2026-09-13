"""Optional, asynchronous burner photo at the start of a data log."""

from PySide6.QtCore import QObject, QSettings, QTimer


class RecordingPhoto(QObject):
    def __init__(self, session, camera):
        super().__init__(session)
        self.session = session
        self.camera = camera
        self.preferences = QSettings('FlowController', 'Camera')
        self.enabled = self.preferences.value('photo_on_recording', False, type=bool)
        self._pending_log = None
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(60000)
        self._timeout.timeout.connect(
            lambda: self._failed('No photo transfer was confirmed within 60 seconds.'))
        session.logging_changed.connect(self._logging_changed)
        camera.captured.connect(self._captured)
        camera.error.connect(self._failed)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.preferences.setValue('photo_on_recording', self.enabled)

    def _logging_changed(self, active, path):
        if not active or not self.enabled:
            return
        state = self.camera.state
        reason = ''
        if self._pending_log is not None:
            reason = 'the previous recording photo is still transferring'
        elif not state.get('selected'):
            reason = 'connect and select a camera in the Camera tab first'
        elif (self.camera.busy or state.get('workflow')
              or state.get('recording') or state.get('bulb_active')):
            reason = 'the camera is busy'
        if reason:
            self.session._log(f'Burner photo skipped for {path}: {reason}. Data logging continues.')
            return
        self._pending_log = str(path)
        self._timeout.start()
        params = {}
        if 'CaptureInRam' in state.get('capabilities', []) and 'capture_in_ram' in state:
            params['capture_in_ram'] = bool(state['capture_in_ram'])
        if not self.camera.action('capture', **params):
            self._failed('The camera did not accept the capture request.')
        elif self._pending_log is not None:
            self.session._log(f'Burner photo requested for {path}.')

    def _captured(self, path):
        if self._pending_log is not None:
            self.session._log(f'Burner photo for {self._pending_log} saved: {path}')
            self._clear()

    def _failed(self, message):
        if self._pending_log is not None:
            self.session._log(
                f'Burner photo failed for {self._pending_log}: {message} Data logging continues.')
            self._clear()

    def _clear(self):
        self._timeout.stop()
        self._pending_log = None
