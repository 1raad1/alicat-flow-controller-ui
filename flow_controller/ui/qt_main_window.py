"""The window the three tabs live in, and the Qt entry point.

The window owns almost no behaviour of its own.  It holds the session, mounts
the three tabs on it, and keeps the two pieces of chrome that must not belong
to any one tab:

* the **safety bar**, in the tab strip's corner.  ``ZERO FUEL`` and ``ZERO ALL``
  are reachable from every tab because an emergency control that depends on
  which tab happens to be open is not an emergency control.
* the **status line**, which is where the run states that outlive a single
  screen — poll rate, log file, LabVIEW listener, sequence, graphs — are
  readable without leaving the tab you are working in.

Everything else is the tabs' business, and every state the chrome shows is read
from a session signal rather than passed between tabs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QVBoxLayout,
)

from .. import APP_VERSION
from ..core.session import FlowSession, SEQ_IDLE, SEQ_RECORDING, SEQ_REPLAYING
from . import qt_theme as theme
from .qt_connection_tab import ConnectionTab
from .qt_logging_tab import LoggingTab
from .qt_operation_tab import OperationTab, SafetyBar
from .qt_settings import SettingsDialog
from .qt_widgets import GlassBackdrop, GlassBar, StatusDot, label

#: How long a one-off message sits in the status line before it clears.  Long
#: enough to be read after looking away, short enough that what is on screen
#: still describes now.
MESSAGE_MS = 12000

#: Status line fields, in reading order, with what they say before anything
#: has happened.  The window keeps these strings itself: a re-theme replaces
#: every label in the window, and the replacements have to open saying what
#: the old ones said.
STATUS_FIELDS = (
    ('poll', 'poll  —'),
    ('log', 'log  OFF'),
    ('udp', 'LabVIEW  off'),
    ('seq', 'seq  idle'),
    ('graphs', 'graphs  idle'),
)

SEQ_WORDS = {SEQ_IDLE: 'idle', SEQ_RECORDING: 'RECORDING',
             SEQ_REPLAYING: 'REPLAYING'}


class MainWindow(QMainWindow):
    """Chrome, three tabs, and an orderly way out."""

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        self.session = session if session is not None else FlowSession(self)
        self.setWindowTitle(f'Alicat Flow Controller v{APP_VERSION}')
        self.resize(theme.scale(1560), theme.scale(940))

        # -- chrome state.  Not held in the widgets, because the widgets do
        #    not survive a re-theme.
        self._status_text = dict(STATUS_FIELDS)
        self._link_kind = 'idle'
        self._link_text = 'not connected'
        self._message = ''
        self._settings = None
        self._theme_pending = False

        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.setInterval(MESSAGE_MS)
        self._message_timer.timeout.connect(lambda: self.show_message(''))

        self._build_ui()
        # Connected once, to the window rather than to any of its widgets, so
        # that rebuilding the view does not leave a second copy of every
        # connection behind it.
        self._connect_session()
        self._sync_from_session()

        if theme.CONFIG_ERROR:
            self.show_message(f'Appearance config ignored ({theme.CONFIG_ERROR}) '
                              '— using defaults.')

    # ================================================================== #
    #  Construction                                                      #
    # ================================================================== #
    def _build_ui(self):
        """Build the whole view from the theme as it currently stands.

        Re-callable, because that is how a re-theme is applied.  Patching the
        live widgets cannot work: layout margins and spacing are read once at
        construction with no way to write them back, and a colour already baked
        into a widget cannot be traced back to the token it came from.
        """
        self.setStyleSheet(theme.STYLESHEET)

        # Everything above this is translucent; the backdrop is the only thing
        # in the window that actually paints a colour.
        root = GlassBackdrop()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_title_bar())
        layout.addWidget(self._build_tabs(), 1)
        layout.addWidget(self._build_status_bar())
        # Replaces and destroys whatever was there before.
        self.setCentralWidget(root)

        self._paint_link()
        self._paint_status()

    def _build_title_bar(self):
        bar = GlassBar('bottom')
        bar.setObjectName('TitleBar')
        row = QHBoxLayout(bar)
        row.setContentsMargins(theme.PAD_XL, theme.PAD_MD + 2,
                               theme.PAD_XL, theme.PAD_MD + 2)
        row.setSpacing(theme.PAD_MD)

        name = QLabel('Alicat Flow Controller')
        name.setObjectName('TitleName')
        row.addWidget(name)
        version = QLabel(f'v{APP_VERSION}')
        version.setObjectName('TitleSub')
        row.addWidget(version)
        row.addSpacing(theme.PAD_LG)
        subtitle = QLabel('Multi-Gas Control  ·  Live Monitoring  ·  Logging')
        subtitle.setObjectName('TitleSub')
        row.addWidget(subtitle)
        row.addStretch(1)

        self._link_dot = StatusDot(theme.TEXT_DIM)
        row.addWidget(self._link_dot)
        self._link_label = label('', color=theme.TEXT_MUTED, size=8,
                                 monospace=True)
        row.addWidget(self._link_label)

        row.addSpacing(theme.PAD_SM)
        settings = QPushButton('⚙')
        settings.setObjectName('IconButton')
        settings.setToolTip('Appearance settings')
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        settings.clicked.connect(self._open_settings)
        row.addWidget(settings)
        return bar

    def _build_tabs(self):
        tabs = QTabWidget()
        tabs.setDocumentMode(True)

        self.connection_tab = ConnectionTab(self.session)
        self.operation_tab = OperationTab(self.session)
        self.logging_tab = LoggingTab(self.session)
        # '&&' because QTabBar reads a single '&' as a mnemonic marker and
        # renders it as an underline on the following letter.
        tabs.addTab(self.connection_tab, 'Connection && Assignment')
        tabs.addTab(self.operation_tab, 'Operation && Monitoring')
        tabs.addTab(self.logging_tab, 'Logging && Graphs')

        self.operation_tab.status.connect(self.show_message)
        self.logging_tab.status.connect(self.show_message)
        # What the plots are doing is a standing description rather than an
        # event, so it gets a field of its own instead of the message area --
        # and a field has to be seeded, because the tab reached its opening
        # state before there was anything connected to hear about it.
        self.logging_tab.graphs_status.connect(
            lambda text: self._set_field('graphs', f'graphs  {text}'))
        self._set_field('graphs', f'graphs  {self.logging_tab.graphs_text()}')

        tabs.setCornerWidget(SafetyBar(self.session),
                             Qt.Corner.TopRightCorner)
        self._tabs = tabs
        return tabs

    def _build_status_bar(self):
        bar = GlassBar('top')
        bar.setObjectName('StatusBar')
        row = QHBoxLayout(bar)
        row.setContentsMargins(theme.PAD_XL, theme.PAD_SM + 1,
                               theme.PAD_XL, theme.PAD_SM + 1)
        row.setSpacing(theme.PAD_XL + 6)

        self._status_labels = {}
        for key, _default in STATUS_FIELDS:
            widget = QLabel(self._status_text[key])
            row.addWidget(widget)
            self._status_labels[key] = widget
        row.addStretch(1)
        self._message_label = QLabel(self._message)
        row.addWidget(self._message_label)
        return bar

    # ================================================================== #
    #  Chrome state                                                      #
    # ================================================================== #
    def _set_field(self, key, text):
        self._status_text[key] = text
        self._paint_status()

    def _paint_status(self):
        # The bars are built after the tabs, and a tab can report state while
        # it is still being constructed, so a state change is allowed to
        # arrive before there is anywhere to show it.  The text is kept either
        # way, and the bar is seeded from it when it does exist.
        labels = getattr(self, '_status_labels', None)
        if labels is None:
            return
        for key, widget in labels.items():
            widget.setText(self._status_text[key])
        self._message_label.setText(self._message)

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
            count = len(self.session.controller_instances)
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
        # Modeless on purpose: picking a colour means watching it land on the
        # real screen, against real readings, not on a preview swatch.
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
            # Mid-run is exactly when the operator must not lose what only the
            # widgets hold — typed setpoints, the sequence panel, the chosen
            # graph series.  The stylesheet is free, so it goes on now; the
            # rebuild waits for the run to stop.
            self.setStyleSheet(theme.STYLESHEET)
            self._theme_pending = True
            self.show_message('Appearance saved — the rest of it applies '
                              'once the run stops.')
            return
        self._rebuild()
        self.show_message('Appearance updated.')

    def _rebuild(self):
        self._theme_pending = False
        index = self._tabs.currentIndex()
        self._build_ui()
        self._tabs.setCurrentIndex(index)

    def _settle_pending_theme(self):
        """Take a deferred re-theme once the run that blocked it has stopped."""
        if not self._theme_pending or self._run_is_live():
            return
        # Deferred: this runs inside a session signal, and the tabs about to be
        # destroyed are further down that signal's own list of receivers.
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
        if self._run_is_live() and not self._confirm_close():
            event.ignore()
            return
        self.session.shutdown()
        super().closeEvent(event)

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
