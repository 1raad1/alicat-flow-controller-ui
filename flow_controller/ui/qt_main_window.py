"""The window the three tabs live in, and the Qt entry point.

The window owns almost no behaviour of its own.  It holds the session, mounts
the three tabs on it, and keeps the three pieces of chrome that must not belong
to any one tab:

* the **control bar**, in the tab strip's corner.  ``SET ALL FLOWS``,
  ``ZERO FUEL`` and ``ZERO ALL`` are reachable from every tab; the zero actions
  must never depend on which tab happens to be open.
* the **status fields**, which are where the run states that outlive a single
  screen — poll rate, log file, LabVIEW listener, sequence, graphs — are
  readable without leaving the tab you are working in.  They sit beside the
  app's name in the title bar: the top of the window is where the eye already
  is for the tab strip and the connection state, and a second strip along the
  bottom edge meant looking away from the run to read about it.
* the **message line** along the bottom, for one-off replies to something the
  operator just did.  It hides itself when there is nothing to say, so an
  empty strip never costs the run a line of screen.

Everything else is the tabs' business, and every state the chrome shows is read
from a session signal rather than passed between tabs.
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
from ..core.session import FlowSession, SEQ_IDLE, SEQ_RECORDING, SEQ_REPLAYING
from . import qt_theme as theme
from .qt_connection_tab import ConnectionTab
from .qt_logging_tab import LoggingTab
from .qt_operation_tab import OperationTab, SafetyBar
from .qt_settings import SettingsDialog
from .qt_widgets import GlassBackdrop, GlassBar, StatusDot, label
from . import qt_win_frame

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

#: How wide the window's own resize border is.  The native frame is gone, so
#: this strip is the only thing left to drag an edge by; it is the frame's own
#: margin rather than an overlay, which is what keeps the tabs and the bars
#: from taking the mouse before it gets here.
RESIZE_MARGIN = 5

#: Which cursor says which edge.  Built once: it is looked up on every mouse
#: move across the strip.
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


#: The chrome glyphs, as the icon font draws them and as ordinary text has to
#: stand in for them.  Windows draws its own caption controls from an icon
#: font whose glyphs share one box and one weight; the punctuation reached for
#: instead -- an en dash, a geometric square, two dingbats -- comes from three
#: different faces at three different sizes, which is why the three controls
#: never sat on the same line as each other.
_ICON_FONTS = ('Segoe Fluent Icons', 'Segoe MDL2 Assets')
GLYPHS = {
    'settings': ('', '⚙'),
    'minimise': ('', '–'),
    'maximise': ('', '□'),
    'restore': ('', '❐'),
    'close': ('', '✕'),
}

#: Design-time size of one control in the title bar's right-hand cluster.
#: Fixed, and the same for all four: the glyphs are still four different
#: shapes, and controls that change size with the shape in them are controls
#: nobody can aim at.
CHROME_BUTTON = (34, 26)

_icon_family = None


def icon_family():
    """The installed icon font, or ``''`` where there is none.

    Asked once and remembered.  It cannot be answered at import: the font
    database needs a live ``QApplication``, and this module is imported to
    build one.
    """
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
    """The stylesheet rule that draws the chrome glyphs.

    Appended to the theme's sheet instead of being written into it, for two
    reasons.  Which font this is cannot be known until there is a
    ``QApplication`` to ask, and the sheet is built at import.  And it has to
    be a *sheet* rule: the sheet already sets ``font-family`` on every widget,
    and in Qt the sheet beats a font set on the widget, so a face applied with
    ``setFont`` here would silently lose and the glyphs would come out blank.

    The size is in the icon font's own terms -- its glyphs fill the em box, so
    it is asked for at about the size the finished mark should be, well under
    what the same number would mean as text.
    """
    family = icon_family()
    face = f"font-family: '{family}'; " if family else ''
    size = theme.font_pt(8 if family else 11)
    return f"""
#IconButton, #WinButton, #WinClose {{ {face}font-size: {size}pt; }}
"""


class WindowFrame(QWidget):
    """The border the operating system used to draw.

    With the native frame dropped there is nothing around the window to take
    hold of, and a control screen that cannot be resized is a control screen
    that cannot be put beside the rig software it is being run against.  So
    the content is inset by :data:`RESIZE_MARGIN` and the strip that leaves is
    handled here, handing the drag straight back to the window manager --
    ``startSystemResize`` is a real system resize, so snapping, live outlines
    and a move between monitors all behave as they always did.

    The inset is dropped while maximised: there is no edge to pull on then,
    and a gutter against the screen edge would only look like a mistake.
    """

    def __init__(self, content, parent=None):
        super().__init__(parent)
        # Without tracking, moves arrive only while a button is held, and the
        # cursor would not change until it was too late to be a hint.
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
    """The app's own title bar, standing in for the one Windows drew.

    Dragging it moves the window and double-clicking it maximises, because
    that is what every title bar does and an operator should not have to be
    told that this one is ours.  The move is handed to the window manager
    rather than rebuilt out of cursor arithmetic, so a drag to the top of the
    screen snaps and a drag onto a second monitor rescales -- both of which
    need the window to keep its native styles, which is ``qt_win_frame``'s
    job.

    The one gesture the window manager will not start for us is the drag off a
    maximised window, because a maximised window has nowhere to move to until
    it has been restored.  So that one is caught here: a press is only
    remembered, and it is the first real *movement* that brings the window
    back down -- under the cursor, at the point along the bar it was taken
    hold of -- and hands the rest of the drag over.
    """

    def __init__(self, window, parent=None):
        # A separate full-width divider sits below this widget.  A hairline
        # painted inside the bar can be overdrawn by translucent child
        # controls, which caused the break near the link and settings cluster.
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
        # A press on a maximised title bar is far more often the start of a
        # click than the start of a drag; Qt's own threshold is what tells
        # the two apart everywhere else, so it tells them apart here.
        if (cursor - start).manhattanLength() < QApplication.startDragDistance():
            return
        self._pressed_at = None
        self._restore_under(cursor)

    def mouseReleaseEvent(self, event):
        self._pressed_at = None
        super().mouseReleaseEvent(event)

    def _restore_under(self, cursor):
        """Come down from maximised without the window jumping off the cursor.

        Keeping the grab where it was on the bar is the whole point: a window
        that restores with its top-left corner under the pointer has moved
        itself, and the operator is then dragging a window they did not aim
        at.  The horizontal hold is kept as a fraction of the width, which is
        what Windows does and what makes the gesture survive the width
        changing on the way down.
        """
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
    """Chrome, three tabs, and an orderly way out."""

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        self.session = session if session is not None else FlowSession(self)
        self.setWindowTitle(f'Alicat Flow Controller v{APP_VERSION}')
        self.resize(theme.scale(1560), theme.scale(940))
        # The window draws its own chrome.  The native bar is a light strip
        # that no theme reaches, sitting above a dark instrument panel and
        # repeating a title the app already shows; dropping it puts the
        # minimise, maximise and close controls on the same line as the name.
        # What the frame was also doing -- moving and resizing the window --
        # is picked up by TitleBar and WindowFrame.
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # -- chrome state.  Not held in the widgets, because the widgets do
        #    not survive a re-theme.
        self._status_text = dict(STATUS_FIELDS)
        self._link_kind = 'idle'
        self._link_text = 'not connected'
        self._message = ''
        self._settings = None
        self._theme_pending = False
        self._native_frame = False

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
        self.setStyleSheet(theme.STYLESHEET + chrome_style())
        # Dropped before anything is built, so a state that arrives mid-build
        # cannot be painted onto the widgets this pass is about to replace.
        self._status_labels = None
        self._status_bar = None

        # Everything above this is translucent; the backdrop is the only thing
        # in the window that actually paints a colour.
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
        # Replaces and destroys whatever was there before.
        self._frame = WindowFrame(root)
        self._frame.set_inset(not self.isMaximized())
        self.setCentralWidget(self._frame)

        self._paint_link()
        self._paint_status()

    def _build_title_bar(self):
        bar = TitleBar(self)
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
        # The standing run states, in place of the strapline that used to sit
        # here.  A description of what the app is is read once, on the first
        # day; what the run is doing is read all day.
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

        # The controls the native bar used to carry, in the order Windows
        # puts them so that muscle memory still lands on the right one.
        row.addSpacing(theme.PAD_SM)
        row.addWidget(self._window_button('minimise', 'Minimise',
                                          self.showMinimized))
        self._max_btn = self._window_button('maximise', 'Maximise',
                                            self.toggle_maximised)
        row.addWidget(self._max_btn)
        # Read from the window rather than remembered: _build_ui runs again on
        # every re-theme, and it can run while the window is maximised.
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
        """Say maximise or restore, whichever the window is not already."""
        button = getattr(self, '_max_btn', None)
        if button is None:
            return
        maximised = self.isMaximized()
        button.setText(glyph('restore' if maximised else 'maximise'))
        button.setToolTip('Restore' if maximised else 'Maximise')

    def toggle_maximised(self):
        """Fill the screen, or come back down to the size before that."""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):
        """Follow the window state, however it was changed.

        The button is not the only way in: Win+Up, a drag to the top of the
        screen and the task bar all maximise too, and a glyph that still says
        'maximise' on a maximised window is worse than no glyph at all.  The
        resize strip goes with it, since a maximised window has no edge to
        pull on.
        """
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        maximised = self.isMaximized()
        frame = getattr(self, '_frame', None)
        if frame is not None:
            frame.set_inset(not maximised)
        self._paint_max_button()

    def showEvent(self, event):
        """Claim the native window behaviours, once there is a window to claim.

        Not in ``__init__``: the styles are set on the real window handle, and
        a widget that has never been shown does not have one yet.
        """
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

        tabs.setCornerWidget(SafetyBar(self.session, self.operation_tab.send_all),
                             Qt.Corner.TopRightCorner)
        self._tabs = tabs
        return tabs

    def _build_status_bar(self):
        """The bottom strip: one-off messages, and nothing else.

        Hidden while it is empty.  The standing fields it used to carry are up
        in the title bar now, and a permanently blank bar across the foot of
        the window would be a line of screen spent saying nothing.
        """
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
        # A tab can report state while it is still being constructed, and the
        # message line is built after the tabs, so a state change is allowed to
        # arrive before there is anywhere to show it.  The text is kept either
        # way, and the chrome is seeded from it once it exists.
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
