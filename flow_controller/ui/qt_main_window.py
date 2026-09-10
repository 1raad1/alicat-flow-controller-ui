"""Main window and Qt entry point.

The window mounts the application tabs and owns controls and status indicators
that must remain available across tabs. Session signals supply their state.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from .. import APP_VERSION
from ..core.optimiser_controller import OptimiserController
from ..core.session import FlowSession, SEQ_IDLE, SEQ_RECORDING, SEQ_REPLAYING
from . import qt_theme as theme
from .qt_connection_tab import ConnectionTab
from .qt_logging_tab import LoggingTab
from .qt_mexa import MexaTab
from .qt_operation_tab import OperationTab, SafetyBar
from ..core.camera import DirectCamera
from .qt_camera import CameraTab
from .qt_settings import SettingsDialog
from .qt_widgets import GlassBackdrop, GlassBar, StatusDot, label
from . import qt_win_frame

#: Time before a one-off status message clears.
MESSAGE_MS = 12000

#: Status fields and their initial text, in display order.
STATUS_FIELDS = (
    ('poll', 'poll  —'),
    ('log', 'log  OFF'),
    ('udp', 'LabVIEW  off'),
    ('seq', 'seq  idle'),
    ('graphs', 'graphs  idle'),
)

SEQ_WORDS = {SEQ_IDLE: 'idle', SEQ_RECORDING: 'RECORDING',
             SEQ_REPLAYING: 'REPLAYING'}

#: Width of the resize border that replaces the native frame.
RESIZE_MARGIN = 5

#: Resize cursor for each edge and corner.
_EDGE_CURSORS = {
    Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.TopEdge | Qt.Edge.LeftEdge: Qt.CursorShape.SizeFDiagCursor,
    Qt.Edge.BottomEdge | Qt.Edge.RightEdge: Qt.CursorShape.SizeFDiagCursor,
    Qt.Edge.TopEdge | Qt.Edge.RightEdge: Qt.CursorShape.SizeBDiagCursor,
    Qt.Edge.BottomEdge | Qt.Edge.LeftEdge: Qt.CursorShape.SizeBDiagCursor,
}


#: Preferred Windows chrome fonts and plain-text fallback glyphs.
_ICON_FONTS = ('Segoe Fluent Icons', 'Segoe MDL2 Assets')
GLYPHS = {
    'settings': ('', '⚙'),
    'minimise': ('', '–'),
    'maximise': ('', '□'),
    'restore': ('', '❐'),
    'close': ('', '✕'),
}

#: Fixed size of each title-bar control.
CHROME_BUTTON = (34, 26)

_icon_family = None


def icon_family():
    """Return the installed icon font, cached after QApplication starts."""
    global _icon_family
    if _icon_family is None:
        installed = set(QFontDatabase.families())
        _icon_family = next((f for f in _ICON_FONTS if f in installed), '')
    return _icon_family


def glyph(name):
    """The character for a chrome control, in whichever font is available."""
    icon, plain = GLYPHS[name]
    return icon if icon_family() else plain


def chrome_style():
    """Return chrome rules after selecting an icon font at runtime.

    The theme stylesheet sets widget fonts, so the chrome font must also be
    applied through a stylesheet rule.
    """
    family = icon_family()
    face = f"font-family: '{family}'; " if family else ''
    size = theme.font_pt(8 if family else 11)
    return f"""
#IconButton, #WinButton, #WinClose {{ {face}font-size: {size}pt; }}
"""


class WindowFrame(QWidget):
    """Resize border for the frameless window.

    Edge drags are passed to the window manager. The border is hidden while
    the window is maximised.
    """

    def __init__(self, content, parent=None):
        super().__init__(parent)
        # Mouse tracking updates the resize cursor before a drag begins.
        self.setMouseTracking(True)
        box = QVBoxLayout(self)
        box.setContentsMargins(*(RESIZE_MARGIN,) * 4)
        box.setSpacing(0)
        box.addWidget(content)
        self._box = box

    def set_inset(self, inset):
        """Show or hide the drag strip, following the window state."""
        margin = RESIZE_MARGIN if inset else 0
        self._box.setContentsMargins(*(margin,) * 4)

    def _edge_at(self, point):
        """The edge flags under ``point``, or ``None`` away from the strip."""
        if self._box.contentsMargins().left() == 0:
            return None
        flags = None
        if point.x() < RESIZE_MARGIN:
            flags = Qt.Edge.LeftEdge
        elif point.x() >= self.width() - RESIZE_MARGIN:
            flags = Qt.Edge.RightEdge
        if point.y() < RESIZE_MARGIN:
            flags = Qt.Edge.TopEdge if flags is None else flags | Qt.Edge.TopEdge
        elif point.y() >= self.height() - RESIZE_MARGIN:
            flags = (Qt.Edge.BottomEdge if flags is None
                     else flags | Qt.Edge.BottomEdge)
        return flags

    def mouseMoveEvent(self, event):
        edge = self._edge_at(event.position().toPoint())
        self.setCursor(_EDGE_CURSORS.get(edge, Qt.CursorShape.ArrowCursor))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        edge = self._edge_at(event.position().toPoint())
        handle = self.window().windowHandle()
        if edge is None or handle is None:
            super().mousePressEvent(event)
            return
        handle.startSystemResize(edge)


class TitleBar(GlassBar):
    """Title bar for moving, maximising and restoring the frameless window.

    The window manager handles normal moves. Dragging a maximised window first
    restores it beneath the pointer, then starts a system move.
    """

    def __init__(self, window, parent=None):
        # A separate divider avoids gaps under translucent child controls.
        super().__init__(None, parent)
        self._window = window
        self._pressed_at = None

    def mousePressEvent(self, event):
        handle = self._window.windowHandle()
        if event.button() != Qt.MouseButton.LeftButton or handle is None:
            super().mousePressEvent(event)
            return
        if self._window.isMaximized():
            self._pressed_at = event.globalPosition().toPoint()
            return
        handle.startSystemMove()

    def mouseMoveEvent(self, event):
        start = self._pressed_at
        if start is None:
            super().mouseMoveEvent(event)
            return
        cursor = event.globalPosition().toPoint()
        # Use Qt's drag threshold to distinguish a click from a restore drag.
        if (cursor - start).manhattanLength() < QApplication.startDragDistance():
            return
        self._pressed_at = None
        self._restore_under(cursor)

    def mouseReleaseEvent(self, event):
        self._pressed_at = None
        super().mouseReleaseEvent(event)

    def _restore_under(self, cursor):
        """Restore while preserving the pointer's relative title-bar position."""
        window = self._window
        held = window.frameGeometry()
        across = ((cursor.x() - held.x()) / held.width()) if held.width() else 0.5
        down = cursor.y() - held.y()

        window.showNormal()
        restored = window.frameGeometry()
        window.move(cursor.x() - round(restored.width() * across),
                    cursor.y() - min(down, self.height()))

        handle = window.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._window.toggle_maximised()
        else:
            super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    """Application shell, tabs and shutdown handling."""

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        self.session = session if session is not None else FlowSession(self)
        self.setWindowTitle(f'Alicat Flow Controller v{APP_VERSION}')
        self.resize(theme.scale(1560), theme.scale(940))
        # TitleBar and WindowFrame replace the native frame and follow the theme.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # Keep chrome state outside widgets because re-theming replaces them.
        self._status_text = dict(STATUS_FIELDS)
        self._link_kind = 'idle'
        self._link_text = 'not connected'
        self._message = ''
        self._settings = None
        self._theme_pending = False
        self._native_frame = False
        self._camera_close_pending = False
        # The campaign and fitting worker outlive widgets replaced by a theme
        # rebuild. No embedded agent, live-control authority or IPC server is
        # started by the desktop application.
        self.optimiser = OptimiserController(self.session, self)
        self.camera = DirectCamera(self)
        self.camera_tab = CameraTab(self.camera)

        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.setInterval(MESSAGE_MS)
        self._message_timer.timeout.connect(lambda: self.show_message(''))

        self._build_ui()
        # Window-level connections survive UI rebuilds without duplication.
        self._connect_session()
        self._sync_from_session()

        if theme.CONFIG_ERROR:
            self.show_message(f'Appearance config ignored ({theme.CONFIG_ERROR}) '
                              '— using defaults.')

    # ================================================================== #
    #  Construction                                                      #
    # ================================================================== #
    def _build_ui(self):
        """Rebuild the view from the current theme."""
        self.setStyleSheet(theme.STYLESHEET + chrome_style())
        # Prevent session updates from painting widgets being replaced.
        self._status_labels = None
        self._status_bar = None

        # Translucent children rely on this backdrop for the window colour.
        root = GlassBackdrop()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_title_bar())
        title_divider = QWidget()
        title_divider.setObjectName('TitleDivider')
        title_divider.setFixedHeight(1)
        layout.addWidget(title_divider)
        layout.addWidget(self._build_tabs(), 1)
        layout.addWidget(self._build_status_bar())
        # setCentralWidget destroys the previous frame.
        self._frame = WindowFrame(root)
        self._frame.set_inset(not self.isMaximized())
        self.setCentralWidget(self._frame)

        self._paint_link()
        self._paint_status()

    def _build_title_bar(self):
        bar = TitleBar(self)
        bar.setObjectName('TitleBar')
        row = QHBoxLayout(bar)
        row.setContentsMargins(theme.PAD_MD, theme.scale(3),
                               theme.PAD_MD, theme.scale(3))
        row.setSpacing(theme.PAD_SM)

        name = QLabel('Alicat Flow Controller')
        name.setObjectName('TitleName')
        row.addWidget(name)
        version = QLabel(f'v{APP_VERSION}')
        version.setObjectName('TitleSub')
        row.addWidget(version)
        row.addSpacing(theme.PAD_SM)
        # Persistent run state remains visible across tabs.
        self._status_labels = {}
        for index, (key, _default) in enumerate(STATUS_FIELDS):
            if index:
                separator = QLabel('·')
                separator.setObjectName('TitleStatusSep')
                row.addWidget(separator)
            widget = QLabel(self._status_text[key])
            widget.setObjectName('TitleStatus')
            row.addWidget(widget)
            self._status_labels[key] = widget
        row.addStretch(1)

        self._link_dot = StatusDot(theme.TEXT_DIM)
        row.addWidget(self._link_dot)
        self._link_label = label('', color=theme.TEXT_MUTED, size=8,
                                 monospace=True)
        row.addWidget(self._link_label)

        row.addSpacing(theme.PAD_SM)
        settings = QPushButton(glyph('settings'))
        settings.setObjectName('IconButton')
        settings.setFixedSize(*(theme.scale(n) for n in CHROME_BUTTON))
        settings.setToolTip('Appearance settings')
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        settings.clicked.connect(self._open_settings)
        row.addWidget(settings)

        # Match the standard Windows caption-button order.
        row.addSpacing(theme.PAD_SM)
        row.addWidget(self._window_button('minimise', 'Minimise',
                                          self.showMinimized))
        self._max_btn = self._window_button('maximise', 'Maximise',
                                            self.toggle_maximised)
        row.addWidget(self._max_btn)
        # Read current state because re-theming can occur while maximised.
        self._paint_max_button()
        close = self._window_button('close', 'Close', self.close)
        close.setObjectName('WinClose')
        row.addWidget(close)
        return bar

    def _window_button(self, name, tip, slot):
        """One of the three controls at the end of the title bar."""
        button = QPushButton(glyph(name))
        button.setObjectName('WinButton')
        button.setFixedSize(*(theme.scale(n) for n in CHROME_BUTTON))
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    def _paint_max_button(self):
        """Show the action available for the current window state."""
        button = getattr(self, '_max_btn', None)
        if button is None:
            return
        maximised = self.isMaximized()
        button.setText(glyph('restore' if maximised else 'maximise'))
        button.setToolTip('Restore' if maximised else 'Maximise')

    def toggle_maximised(self):
        """Toggle maximised state."""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):
        """Update custom chrome after any system window-state change."""
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        maximised = self.isMaximized()
        frame = getattr(self, '_frame', None)
        if frame is not None:
            frame.set_inset(not maximised)
        self._paint_max_button()

    def showEvent(self, event):
        """Enable native behaviour after Qt creates the window handle."""
        super().showEvent(event)
        if not self._native_frame:
            self._native_frame = qt_win_frame.enable(self)

    def nativeEvent(self, event_type, message):
        """Let the frame keep the window's chrome out of the client area."""
        answer = qt_win_frame.handle_native_event(self, event_type, message)
        if answer is not None:
            return answer
        return super().nativeEvent(event_type, message)

    def _build_tabs(self):
        tabs = QTabWidget()
        tabs.setDocumentMode(True)

        self.connection_tab = ConnectionTab(self.session)
        self.operation_tab = OperationTab(self.session, optimiser=self.optimiser,
                                          camera=self.camera)
        self.optimiser_pane = self.operation_tab.optimiser_pane
        self.logging_tab = LoggingTab(self.session)
        self.mexa_tab = MexaTab(self.session.mexa)
        # '&&' because QTabBar reads a single '&' as a mnemonic marker and
        # renders it as an underline on the following letter.
        tabs.addTab(self.connection_tab, 'Connection && Assignment')
        tabs.addTab(self.operation_tab, 'Operation && Monitoring')
        tabs.addTab(self.logging_tab, 'Logging && Graphs')
        tabs.addTab(self.mexa_tab, 'MEXA analyser')
        tabs.addTab(self.camera_tab, 'Camera')
        self.operation_tab.camera_card.settings_requested.connect(
            lambda: tabs.setCurrentWidget(self.camera_tab))

        self.operation_tab.status.connect(self.show_message)
        self.logging_tab.status.connect(self.show_message)
        # Seed graph state because its initial signal predates this connection.
        self.logging_tab.graphs_status.connect(
            lambda text: self._set_field('graphs', f'graphs  {text}'))
        self._set_field('graphs', f'graphs  {self.logging_tab.graphs_text()}')

        tabs.setCornerWidget(SafetyBar(self.session, self.operation_tab.send_all),
                             Qt.Corner.TopRightCorner)
        self._tabs = tabs
        return tabs

    def _build_status_bar(self):
        """Build the one-off message strip, hidden while empty."""
        bar = GlassBar('top')
        bar.setObjectName('StatusBar')
        row = QHBoxLayout(bar)
        row.setContentsMargins(theme.PAD_XL, theme.PAD_SM + 1,
                               theme.PAD_XL, theme.PAD_SM + 1)
        row.setSpacing(theme.PAD_MD)

        self._message_label = QLabel(self._message)
        self._message_label.setWordWrap(False)
        row.addWidget(self._message_label, 1)
        bar.setVisible(bool(self._message))
        self._status_bar = bar
        return bar

    # ================================================================== #
    #  Chrome state                                                      #
    # ================================================================== #
    def _set_field(self, key, text):
        self._status_text[key] = text
        self._paint_status()

    def _paint_status(self):
        # Tabs may report state before the message line has been built.
        labels = getattr(self, '_status_labels', None)
        if labels is None:
            return
        for key, widget in labels.items():
            widget.setText(self._status_text[key])
        bar = getattr(self, '_status_bar', None)
        if bar is not None:
            self._message_label.setText(self._message)
            bar.setVisible(bool(self._message))

    def show_message(self, text):
        """Put a one-off line in the status bar, or clear it with ``''``."""
        self._message = text
        self._paint_status()
        self._message_timer.stop()
        if text:
            self._message_timer.start()

    def _paint_link(self):
        dot = getattr(self, '_link_dot', None)
        if dot is None:
            return
        color = {'ok': theme.OK, 'busy': theme.WARN}.get(self._link_kind,
                                                         theme.TEXT_DIM)
        dot.set_color(color)
        self._link_label.setText(self._link_text)

    def _set_link(self, kind, text):
        self._link_kind = kind
        self._link_text = text
        self._paint_link()

    # ================================================================== #
    #  Session                                                           #
    # ================================================================== #
    def _connect_session(self):
        session = self.session
        session.connection_changed.connect(self._on_connection)
        session.connecting_changed.connect(self._on_connecting)
        session.monitoring_changed.connect(self._on_monitoring)
        session.poll_rate.connect(self._on_poll_rate)
        session.logging_changed.connect(self._on_logging)
        session.udp_changed.connect(self._on_udp)
        session.sequence_state_changed.connect(self._on_sequence_state)
        session.sequence_progress.connect(self._on_sequence_progress)

    def _sync_from_session(self):
        """Open showing the run as it stands, not as it started."""
        session = self.session
        self._on_connection(session.controllers_connected)
        self._on_logging(session.logging_active, session.log_path)
        self._on_sequence_state(session.sequence_state)

    def _on_connection(self, connected):
        if connected:
            count = len(self.session.assigned_units())
            port = self.session.port or '—'
            self._set_link('ok', f"{count} controller{'' if count == 1 else 's'}"
                                 f" · {port} · {self.session.baudrate} baud")
        else:
            self._set_link('idle', 'not connected')
            self._set_field('poll', 'poll  —')
        self._settle_pending_theme()

    def _on_connecting(self, busy):
        if busy:
            self._set_link('busy', 'connecting…')

    def _on_monitoring(self, active):
        if not active:
            self._set_field('poll', 'poll  —')
        self._settle_pending_theme()

    def _on_poll_rate(self, hz, ms):
        self._set_field('poll', f'poll  {hz:.1f} Hz  ({ms:.0f} ms/pass)')

    def _on_logging(self, active, path):
        name = Path(path).name if path else ''
        self._set_field('log', f'log  REC  {name}' if active else 'log  OFF')
        self._settle_pending_theme()

    def _on_udp(self, active, message):
        self._set_field('udp', f"LabVIEW  {message if active else 'off'}")

    def _on_sequence_state(self, state):
        word = SEQ_WORDS.get(state, state)
        self._set_field('seq', f'seq  {word}')
        self._settle_pending_theme()

    def _on_sequence_progress(self, position, duration):
        word = SEQ_WORDS.get(self.session.sequence_state, '')
        span = f'{position:.1f} s' if duration <= 0 else \
               f'{position:.1f} / {duration:.1f} s'
        self._set_field('seq', f'seq  {word}  {span}')

    # ================================================================== #
    #  Re-theming                                                        #
    # ================================================================== #
    def _open_settings(self):
        # Keep this modeless so theme changes remain visible behind it.
        if self._settings is None:
            self._settings = SettingsDialog(theme.CONFIG, self)
            self._settings.applied.connect(self._retheme)
            self._settings.finished.connect(lambda _result: self._forget_settings())
        self._settings.show()
        self._settings.raise_()

    def _forget_settings(self):
        self._settings = None

    def _retheme(self, config):
        theme.apply(config)
        if self._run_is_live():
            # Defer widget replacement during a run to preserve unsaved UI state.
            self.setStyleSheet(theme.STYLESHEET + chrome_style())
            self._theme_pending = True
            self.show_message('Appearance saved — the rest of it applies '
                              'once the run stops.')
            return
        self._rebuild()
        self.show_message('Appearance updated.')

    def _rebuild(self):
        self._theme_pending = False
        index = self._tabs.currentIndex()
        self.operation_tab.camera_card.shutdown()
        # Keep camera controls and their unsaved settings while replacing tabs.
        self.camera_tab.setParent(self)
        self._build_ui()
        self._tabs.setCurrentIndex(index)

    def _settle_pending_theme(self):
        """Take a deferred re-theme once the run that blocked it has stopped."""
        if not self._theme_pending or self._run_is_live():
            return
        # Wait until the current session signal finishes dispatching.
        QTimer.singleShot(0, self._rebuild_if_still_idle)

    def _rebuild_if_still_idle(self):
        if self._theme_pending and not self._run_is_live():
            self._rebuild()
            self.show_message('Appearance updated.')

    # ================================================================== #
    #  Shutdown                                                          #
    # ================================================================== #
    def _run_is_live(self):
        session = self.session
        return bool(session.controllers_connected or session.is_monitoring
                    or session.logging_active
                    or session.sequence_state != SEQ_IDLE)

    def closeEvent(self, event):
        if (not self._camera_close_pending
                and self._run_is_live() and not self._confirm_close()):
            event.ignore()
            return
        if not self.optimiser.shutdown():
            QMessageBox.critical(
                self, 'Optimiser calculation finishing',
                'Cancellation requested. Wait for the optimiser calculation '
                'to finish, then close again. Flow control is unchanged.')
            event.ignore()
            return
        if self.camera.shutdown() is False:
            self.show_message('Camera is releasing the USB connection…')
            if not self._camera_close_pending:
                self._camera_close_pending = True
                self._camera_close_timer = QTimer(self)
                self._camera_close_timer.setInterval(100)
                self._camera_close_timer.timeout.connect(self._finish_camera_close)
                self._camera_close_timer.start()
            event.ignore()
            return
        if self.session.shutdown() is False:
            self.show_message('Cancelling history export. Close again when it finishes.')
            event.ignore()
            return
        self.operation_tab.camera_card.shutdown()
        self.camera_tab.shutdown()
        super().closeEvent(event)

    def _finish_camera_close(self):
        if self.camera.shutdown():
            self._camera_close_timer.stop()
            self.close()

    def _confirm_close(self):
        """Ask before closing on a live run, and say what closing does not do."""
        session = self.session
        running = []
        if session.is_monitoring:
            running.append('the live monitor is running')
        if session.controllers_connected:
            running.append('the controllers are connected')
        if session.logging_active:
            running.append('a log file is open')
        if session.sequence_state != SEQ_IDLE:
            running.append('a sequence is '
                           + SEQ_WORDS.get(session.sequence_state, 'running'))

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('Close Flow Controller?')
        box.setText('Close while ' + ', '.join(running) + '?')
        box.setInformativeText(
            'Closing stops monitoring, logging and any sequence, and releases '
            'the serial port.\n\nIt does NOT zero the controllers: whatever '
            'setpoint each one is holding, it keeps holding. Use ZERO ALL '
            'first if the rig should be shut down.')
        box.setStandardButtons(QMessageBox.StandardButton.Cancel
                               | QMessageBox.StandardButton.Close)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Close


def main(argv=None):
    """Run the Qt application.  Returns the process exit code."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName('Alicat Flow Controller')
    app.setApplicationVersion(APP_VERSION)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
