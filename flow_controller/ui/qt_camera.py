"""Direct-USB camera controls and shared burner preview widgets."""

from pathlib import Path

from PySide6.QtCore import QRect, QSize, QSettings, QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPainter
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QLabel,
    QLineEdit, QListWidget, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .qt_widgets import Card, row


def _key(value):
    return ''.join(c for c in str(value).casefold() if c.isalnum())


class CameraImage(QWidget):
    """Paint a shared frame without allowing its dimensions to drive layout."""

    DEFAULT_ASPECT_RATIO = 16 / 9
    INLINE_MINIMUM_HEIGHT = 120
    INLINE_MAXIMUM_HEIGHT = 420
    PREFERRED_WIDTH = 320

    def __init__(self, camera, parent=None, *, fit_height_to_width=True):
        super().__init__(parent)
        self.image = QImage()
        self._fit_height_to_width = fit_height_to_width
        policy = QSizePolicy.Policy.Expanding
        vertical_policy = (QSizePolicy.Policy.Preferred
                           if fit_height_to_width else policy)
        size_policy = QSizePolicy(policy, vertical_policy)
        size_policy.setHeightForWidth(fit_height_to_width)
        self.setSizePolicy(size_policy)
        if fit_height_to_width:
            self.setMinimumSize(160, self.INLINE_MINIMUM_HEIGHT)
            self.setMaximumHeight(self.INLINE_MAXIMUM_HEIGHT)
        else:
            self.setMinimumSize(160, 90)
        self.setAccessibleName('Burner camera image')
        camera.frame_received.connect(self.set_image)

    def set_image(self, image):
        previous_ratio = self._aspect_ratio()
        self.image = QImage(image)
        if self._aspect_ratio() != previous_ratio:
            self.updateGeometry()
        self.update()

    def _aspect_ratio(self):
        if not self.image.isNull() and self.image.height() > 0:
            return self.image.width() / self.image.height()
        return self.DEFAULT_ASPECT_RATIO

    def heightForWidth(self, width):
        height = round(max(1, width) / self._aspect_ratio())
        if self._fit_height_to_width:
            return max(self.INLINE_MINIMUM_HEIGHT,
                       min(self.INLINE_MAXIMUM_HEIGHT, height))
        return height

    def hasHeightForWidth(self):
        return self._fit_height_to_width

    def sizeHint(self):
        width = self.PREFERRED_WIDTH
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        if self._fit_height_to_width:
            return QSize(160, self.INLINE_MINIMUM_HEIGHT)
        return QSize(160, 90)

    def image_rect(self):
        """Return the aspect-fit destination used to paint the current frame."""
        if self.image.isNull() or self.width() <= 0 or self.height() <= 0:
            return QRect()
        size = self.image.size().scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - size.width()) // 2,
                     (self.height() - size.height()) // 2,
                     size.width(), size.height())

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), '#11151b')
        if self.image.isNull():
            painter.setPen('#aab4c0')
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             'No camera image\nStart live view to begin')
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.image_rect(), self.image)


class _CameraPopout(QWidget):
    def __init__(self, camera, image, status, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle('Burner camera')
        self.resize(960, 640)
        layout = QVBoxLayout(self)
        self.preview = CameraImage(camera, fit_height_to_width=False)
        self.preview.set_image(image)
        layout.addWidget(self.preview, 1)
        self.status = QLabel(status)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        camera.status_changed.connect(self.status.setText)
        camera.error.connect(self.status.setText)


class BurnerCameraCard(Card):
    """Compact passive preview used on the operation screen."""

    settings_requested = Signal()

    def __init__(self, camera, parent=None):
        super().__init__('Burner camera', parent=parent,
                         help_text='Live USB camera view for burner observation.')
        self.camera = camera
        self._busy = bool(getattr(camera, 'busy', False))
        self._state = dict(getattr(camera, 'state', {}) or {})
        self.preview = CameraImage(camera)
        self.add(self.preview)
        self.feed_status = QLabel('Preview stopped')
        self.feed_status.setWordWrap(True)
        self.add(self.feed_status)

        self.start_button = QPushButton('Start live view')
        self.stop_button = QPushButton('Stop live view')
        self.capture_button = QPushButton('Capture')
        self.start_button.clicked.connect(camera.start_preview)
        self.stop_button.clicked.connect(camera.stop_preview)
        self.capture_button.clicked.connect(self._capture)
        self.add(row(self.start_button, self.stop_button,
                     self.capture_button, None))
        self.pop_button = QPushButton('Pop out')
        self.pop_button.clicked.connect(self.pop_out)
        self.add_header_widget(self.pop_button)
        self.settings_button = QPushButton('Camera controls')
        self.settings_button.clicked.connect(self.settings_requested.emit)
        self.add_header_widget(self.settings_button)
        self.popout = None

        camera.status_changed.connect(self.feed_status.setText)
        camera.error.connect(self.feed_status.setText)
        camera.busy_changed.connect(self._set_busy)
        camera.state_changed.connect(self._set_state)
        self._sync()

    def _has(self, *names):
        caps = {_key(value) for value in self._state.get('capabilities', [])}
        return any(_key(name) in caps for name in names)

    def _capture(self):
        params = {}
        if self._has('CaptureInRam') and 'capture_in_ram' in self._state:
            params['capture_in_ram'] = bool(self._state['capture_in_ram'])
        self.camera.action('capture', **params)

    def _set_busy(self, busy):
        self._busy = bool(busy)
        self._sync()

    def _set_state(self, state):
        self._state = dict(state or {})
        self._sync()

    def _sync(self):
        selected = bool(self._state.get('selected'))
        blocked = self._busy or bool(self._state.get('busy')) or bool(self._state.get('workflow'))
        self.start_button.setEnabled(
            selected and self._has('LiveView') and not blocked)
        self.stop_button.setEnabled(
            selected and self._has('LiveView'))
        self.capture_button.setEnabled(selected and not blocked)

    def pop_out(self):
        if self.popout is None:
            self.popout = _CameraPopout(
                self.camera, self.preview.image, self.feed_status.text(),
                self.window())
        self.popout.show()
        self.popout.raise_()
        self.popout.activateWindow()

    def shutdown(self):
        if self.popout is not None:
            self.popout.close()
            self.popout.deleteLater()
            self.popout = None


class CameraTab(QWidget):
    """Native controls for cameras discovered over direct USB."""

    FPS_VALUES = (5, 10, 15, 30)

    def __init__(self, camera, parent=None):
        super().__init__(parent)
        self.camera = camera
        self.preferences = QSettings('FlowController', 'Camera')
        self._state = dict(getattr(camera, 'state', {}) or {})
        self._busy = bool(getattr(camera, 'busy', False))
        self._gated = []
        self._property_editors = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        connection = Card('USB camera', collapsible=False)
        self.discover_button = QPushButton('Discover USB cameras')
        self.discover_button.clicked.connect(camera.connect_camera)
        self.disconnect_button = QPushButton('Disconnect')
        self.disconnect_button.clicked.connect(lambda: camera.action('disconnect'))
        connection.add(row(self.discover_button, self.disconnect_button, None))
        self.cameras = QComboBox()
        self.cameras.setMinimumContentsLength(18)
        self.cameras.activated.connect(self._select_camera)
        connection.add(row(QLabel('Camera'), self.cameras, None))
        self.battery = QLabel('Battery —')
        self.connection_status = QLabel('No USB camera selected')
        self.connection_status.setWordWrap(True)
        connection.add(row(self.battery, self.connection_status, stretch_at=1))
        layout.addWidget(connection)

        self.preview_card = BurnerCameraCard(camera)
        layout.addWidget(self.preview_card)

        capture = Card('Capture and live view')
        self.capture_button = QPushButton('Capture')
        self.capture_button.clicked.connect(lambda: self._capture('capture'))
        self._gated.append((self.capture_button, ()))
        self.capture_no_af_button = QPushButton('Capture without AF')
        self.capture_no_af_button.clicked.connect(
            lambda: self._capture('capture_no_af'))
        self._gated.append((self.capture_no_af_button, ('CaptureNoAf',)))
        self.live_start_button = QPushButton('Start live view')
        self.live_start_button.clicked.connect(camera.start_preview)
        self.live_stop_button = QPushButton('Stop live view')
        self.live_stop_button.clicked.connect(camera.stop_preview)
        self._gated.extend([
            (self.live_start_button, ('LiveView',)),
            (self.live_stop_button, ('LiveView',)),
        ])
        capture.add(row(self.capture_button, self.capture_no_af_button,
                        self.live_start_button, self.live_stop_button, None))
        self.video_start_button = self._button(
            'Start video', 'video_start', 'RecordMovie', 'Video')
        self.video_stop_button = self._button(
            'Stop video', 'video_stop', 'RecordMovie', 'Video')
        self.bulb_start_button = self._button(
            'Start bulb exposure', 'bulb_start', 'Bulb')
        self.bulb_stop_button = self._button(
            'Stop bulb exposure', 'bulb_stop', 'Bulb')
        capture.add(row(self.video_start_button, self.video_stop_button,
                        self.bulb_start_button, self.bulb_stop_button, None))
        self.lock_button = self._button(
            'Lock camera', 'camera_lock', 'CanLockFocus')
        self.unlock_button = self._button(
            'Unlock camera', 'camera_unlock', 'CanLockFocus')
        capture.add(row(self.lock_button, self.unlock_button, None))
        self.capture_in_ram = QCheckBox('Capture in camera RAM')
        self.capture_in_ram.toggled.connect(self._set_capture_target)
        self._gated.append((self.capture_in_ram, ('CaptureInRam',)))
        self.autofocus_before_capture = QCheckBox('Autofocus before live-view capture')
        self.autofocus_before_capture.setToolTip(
            'Like digiCamControl: focus separately, then capture without shutter autofocus.')
        self._gated.append((self.autofocus_before_capture, ('LiveView',)))
        capture.add(row(self.capture_in_ram, self.autofocus_before_capture, None))
        layout.addWidget(capture)

        focus = Card('Focus')
        self.autofocus_button = self._button(
            'Autofocus', 'autofocus', 'LiveView', 'AutoFocus')
        self.focus_minus_button = QPushButton('Focus −')
        self.focus_minus_button.clicked.connect(
            lambda: camera.action('focus', step=-int(self.focus_step.currentText())))
        self.focus_plus_button = QPushButton('Focus +')
        self.focus_plus_button.clicked.connect(
            lambda: camera.action('focus', step=int(self.focus_step.currentText())))
        self._gated.extend([
            (self.focus_minus_button, ('LiveView', 'SimpleManualFocus')),
            (self.focus_plus_button, ('LiveView', 'SimpleManualFocus')),
        ])
        self.focus_step = QComboBox()
        self.focus_step.addItems(['1', '2', '3'])
        focus.add(row(self.autofocus_button, QLabel('Step'), self.focus_step,
                      self.focus_minus_button,
                      self.focus_plus_button, None))
        self.focus_x, self.focus_y = QSpinBox(), QSpinBox()
        for spin in (self.focus_x, self.focus_y):
            spin.setRange(0, 65535)
        self.focus_point_button = QPushButton('Set focus point')
        self.focus_point_button.clicked.connect(
            lambda: camera.action('focus_point', x=self.focus_x.value(),
                                  y=self.focus_y.value()))
        self._gated.append((self.focus_point_button, ('LiveView',)))
        focus.add(row(QLabel('X (pixels)'), self.focus_x,
                      QLabel('Y (pixels)'), self.focus_y,
                      self.focus_point_button, None))
        layout.addWidget(focus)

        storage = Card('Capture storage')
        pictures = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.PicturesLocation)
        default_output = Path(pictures or str(Path.home() / 'Pictures')) / 'Flow Controller'
        output = str(self.preferences.value('output', str(default_output)))
        self.output_path = QLineEdit(output)
        self.output_path.setToolTip(output)
        self.output_path.textChanged.connect(self.output_path.setToolTip)
        self.output_path.editingFinished.connect(self._commit_output)
        self.output_button = QPushButton('Browse…')
        self.output_button.clicked.connect(self._browse_output)
        storage.add(row(QLabel('Output folder'), self.output_path,
                        self.output_button, stretch_at=1))
        self.fps = QComboBox()
        self.fps.addItems([str(value) for value in self.FPS_VALUES])
        saved_fps = int(self.preferences.value('fps', 10))
        self.fps.setCurrentText(str(saved_fps if saved_fps in self.FPS_VALUES else 10))
        self.fps.currentTextChanged.connect(self._set_fps)
        storage.add(row(QLabel('Preview rate'), self.fps,
                        QLabel('frames/s'), None))
        self.recent = QListWidget()
        self.recent.setMaximumHeight(110)
        self.open_capture_button = QPushButton('Open selected capture')
        self.open_capture_button.clicked.connect(self._open_capture)
        storage.add(self.recent)
        storage.add(row(self.open_capture_button, None))
        layout.addWidget(storage)

        properties = Card('Device properties')
        self.refresh_properties_button = QPushButton('Refresh settings')
        self.refresh_properties_button.clicked.connect(
            lambda: camera.action('refresh'))
        self._gated.append((self.refresh_properties_button, ()))
        properties.add_header_widget(self.refresh_properties_button)
        self.properties_table = QTableWidget(0, 4)
        self.properties_table.setHorizontalHeaderLabels(
            ['Property', 'Current', 'New value', ''])
        self.properties_table.verticalHeader().setVisible(False)
        self.properties_table.horizontalHeader().setStretchLastSection(True)
        properties.add(self.properties_table)
        layout.addWidget(properties)

        workflows = Card('Automated capture workflows')
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 86400.0)
        self.interval.setValue(5.0)
        self.interval.setSuffix(' s')
        self.count = QSpinBox()
        self.count.setRange(1, 10000)
        self.count.setValue(10)
        self.timelapse_af = QCheckBox('Autofocus each capture')
        self.timelapse_af.setChecked(True)
        self.timelapse_button = QPushButton('Start timelapse')
        self.timelapse_button.clicked.connect(self._start_timelapse)
        workflows.add(row(QLabel('Interval'), self.interval, QLabel('Count'),
                          self.count, self.timelapse_af,
                          self.timelapse_button, None))
        self.bracket_property = QLineEdit('exposurecompensation')
        self.bracket_values = QLineEdit('-1, 0, 1')
        self.bracket_af = QCheckBox('Autofocus each capture')
        self.bracket_af.setChecked(True)
        self.bracket_button = QPushButton('Start bracket')
        self.bracket_button.clicked.connect(self._start_bracket)
        bracket = QFormLayout()
        bracket.addRow('Property', self.bracket_property)
        bracket.addRow('Values (comma-separated)', self.bracket_values)
        workflows.add_layout(bracket)
        workflows.add(row(self.bracket_af, self.bracket_button, None))
        self.stop_workflow_button = QPushButton('Stop workflow')
        self.stop_workflow_button.clicked.connect(
            lambda: camera.action('workflow_stop'))
        workflows.add(row(self.stop_workflow_button, None))
        self._gated.extend([
            (self.timelapse_button, ()),
            (self.bracket_button, ()),
            (self.stop_workflow_button, ()),
        ])
        layout.addWidget(workflows)
        layout.addStretch(1)

        camera.status_changed.connect(self.connection_status.setText)
        camera.error.connect(self.connection_status.setText)
        camera.busy_changed.connect(self._set_busy)
        camera.state_changed.connect(self._set_state)
        camera.captured.connect(self._captured)
        camera.action_finished.connect(self._action_finished)
        # Safe facade configuration only: no USB discovery or device opening.
        camera.set_fps(int(self.fps.currentText()))
        camera.set_output_directory(output)
        self._set_state(self._state)

    def _button(self, text, action, *capabilities):
        button = QPushButton(text)
        button.clicked.connect(lambda: self.camera.action(action))
        self._gated.append((button, capabilities))
        return button

    def _capture(self, action):
        params = {}
        if action == 'capture' and self.autofocus_before_capture.isChecked():
            params['autofocus_before_capture'] = True
        if self._has('CaptureInRam') and 'capture_in_ram' in self._state:
            params['capture_in_ram'] = bool(self._state['capture_in_ram'])
        self.camera.action(action, **params)

    def _set_capture_target(self, checked):
        if self._has('CaptureInRam'):
            self.camera.action('capture_target', capture_in_ram=bool(checked))

    def _has(self, *names):
        caps = {_key(value) for value in self._state.get('capabilities', [])}
        return any(_key(name) in caps for name in names)

    def _select_camera(self, index):
        identifier = self.cameras.itemData(index)
        if identifier:
            self.camera.select_camera(str(identifier))

    def _browse_output(self):
        selected = QFileDialog.getExistingDirectory(
            self, 'Camera output folder', self.output_path.text())
        if selected:
            self.output_path.setText(selected)
            self.preferences.setValue('output', selected)
            self.camera.set_output_directory(selected)

    def _commit_output(self):
        value = self.output_path.text().strip()
        if not value:
            return
        try:
            self.camera.set_output_directory(value)
        except ValueError as exc:
            self.connection_status.setText(str(exc))
            return
        self.preferences.setValue('output', value)

    def _set_fps(self, text):
        if text:
            value = int(text)
            self.preferences.setValue('fps', value)
            self.camera.set_fps(value)

    def _start_timelapse(self):
        self.camera.action('timelapse_start', interval=self.interval.value(),
                           count=self.count.value(),
                           autofocus=self.timelapse_af.isChecked())

    def _start_bracket(self):
        values = [v.strip() for v in self.bracket_values.text().split(',')
                  if v.strip()]
        self.camera.action('bracket_start',
                           property=self.bracket_property.text().strip(),
                           values=values,
                           autofocus=self.bracket_af.isChecked())

    def _set_busy(self, busy):
        self._busy = bool(busy)
        self._sync()

    def _set_state(self, state):
        self._state = dict(state or {})
        selected = str(self._state.get('selected') or '')
        self.cameras.blockSignals(True)
        self.cameras.clear()
        for camera in self._state.get('cameras', []):
            if isinstance(camera, dict):
                identifier = str(camera.get('id', camera.get('name', '')))
                name = str(camera.get('name', identifier))
            else:
                identifier = name = str(camera)
            self.cameras.addItem(name, identifier)
        match = self.cameras.findData(selected)
        if match >= 0:
            self.cameras.setCurrentIndex(match)
        self.cameras.blockSignals(False)
        if 'capture_in_ram' in self._state:
            self.capture_in_ram.blockSignals(True)
            self.capture_in_ram.setChecked(bool(self._state['capture_in_ram']))
            self.capture_in_ram.blockSignals(False)
        battery = self._state.get('battery')
        self.battery.setText('Battery —' if battery is None else f'Battery {battery}%')
        self._rebuild_properties(self._state.get('properties', []))
        self._sync()

    def _sync(self):
        selected = bool(self._state.get('selected'))
        workflow = bool(self._state.get('workflow'))
        blocked = self._busy or bool(self._state.get('busy')) or workflow
        self.discover_button.setEnabled(not blocked)
        self.cameras.setEnabled(not blocked and self.cameras.count() > 0)
        self.disconnect_button.setEnabled(not self._busy and selected)
        for widget, capabilities in self._gated:
            widget.setEnabled(not blocked and selected
                              and (not capabilities or self._has(*capabilities)))
        # Ending an active device mode must stay reachable while the device
        # reports itself busy; these commands are the way out of that state.
        self.live_stop_button.setEnabled(selected and self._has('LiveView'))
        self.video_stop_button.setEnabled(
            selected and self._has('RecordMovie', 'Video')
            and not self._busy and not workflow)
        self.bulb_stop_button.setEnabled(
            selected and self._has('Bulb')
            and not self._busy and not workflow)
        writable = any(not prop.get('readonly', False) and prop.get('values')
                       for prop in self._state.get('properties', [])
                       if isinstance(prop, dict))
        self.bracket_button.setEnabled(not blocked and selected and writable)
        self.stop_workflow_button.setEnabled(selected and workflow)
        self.output_path.setEnabled(not blocked)
        self.output_button.setEnabled(not blocked)
        self.fps.setEnabled(not blocked)
        self.open_capture_button.setEnabled(self.recent.currentItem() is not None)
        specs = {str(p['name']): p for p in self._state.get('properties', [])
                 if isinstance(p, dict) and p.get('name')}
        for row_number, (name, editor) in enumerate(self._property_editors.items()):
            writable = not specs[name].get('readonly', False) and bool(specs[name].get('values'))
            editor.setEnabled(selected and not blocked and writable)
            self.properties_table.cellWidget(row_number, 3).setEnabled(
                selected and not blocked and writable)

    def _rebuild_properties(self, properties):
        self.properties_table.setRowCount(0)
        self._property_editors.clear()
        can_set = (bool(self._state.get('selected')) and not self._busy
                   and not self._state.get('busy') and not self._state.get('workflow'))
        for spec in properties or []:
            if not isinstance(spec, dict) or not spec.get('name'):
                continue
            row_number = self.properties_table.rowCount()
            self.properties_table.insertRow(row_number)
            name = str(spec['name'])
            self.properties_table.setItem(
                row_number, 0, QTableWidgetItem(str(spec.get('label', name))))
            self.properties_table.setItem(
                row_number, 1, QTableWidgetItem(str(spec.get('value', ''))))
            for column in (0, 1):
                self.properties_table.item(row_number, column).setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            values = [str(value) for value in spec.get('values', [])]
            if values:
                editor = QComboBox()
                editor.addItems(values)
                editor.setCurrentText(str(spec.get('value', '')))
            else:
                editor = QLineEdit(str(spec.get('value', '')))
            readonly = bool(spec.get('readonly', False))
            editor.setEnabled(not readonly)
            self.properties_table.setCellWidget(row_number, 2, editor)
            apply = QPushButton('Apply')
            apply.setEnabled(can_set and not readonly)
            apply.clicked.connect(
                lambda _checked=False, key=name, field=editor:
                self._apply_property(key, field))
            self.properties_table.setCellWidget(row_number, 3, apply)
            self._property_editors[name] = editor

    def _apply_property(self, name, editor):
        value = editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        self.camera.action('set_property', name=name, value=value)

    def _captured(self, path):
        self.recent.insertItem(0, str(path))
        self.recent.setCurrentRow(0)
        self.open_capture_button.setEnabled(True)
        self.connection_status.setText(f'Photo saved: {Path(path).name}')

    def _open_capture(self):
        item = self.recent.currentItem()
        if item is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item.text()))

    def _action_finished(self, name, result):
        if name in ('capture', 'capture_no_af'):
            self.connection_status.setText('Capture requested; waiting for the photo…')
            return
        if name == 'set_property' and isinstance(result, dict) and 'value' in result:
            self.connection_status.setText(
                f"{result.get('property', 'Camera setting')}: {result['value']}")
            return
        title = str(name).replace('_', ' ').capitalize()
        if isinstance(result, (str, int, float)) and str(result):
            self.connection_status.setText(f'{title}: {result}')
        else:
            self.connection_status.setText(f'{title} completed')

    def shutdown(self):
        self.preview_card.shutdown()
        return True
