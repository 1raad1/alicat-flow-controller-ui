"""Record, edit and replay a run as curves of setpoint against time.

Three widgets, in the order the operator meets them:

:class:`TimelineStrip`
    The clock.  It is the same strip whether a run is being recorded or
    replayed, because the operator's question is the same either way -- how
    far in are we -- and giving each mode its own indicator would mean
    learning two.

:class:`CurveEditor`
    The curves themselves, one per controller, editable by dragging.  Only
    the *selected* track can be edited; the rest are drawn dim for context.
    Editing one line of a burner while unable to see what the others are
    doing at that instant is how you build a sequence that runs the rig
    somewhere nobody intended.  Selecting nothing gives the overview: all
    controllers overlaid at equal weight, which is what a sequence just
    opened wants to be read as.

:class:`SequencePanel`
    The transport, the track list and the two above, bound to
    :class:`~flow_controller.core.session.FlowSession`.

The panel never writes to hardware.  Recording listens; replay is started on
the session, which plays back through the ordinary setpoint queue, so the
zero lock and the E-STOP hold over a replay exactly as they do over a
setpoint the operator typed.
"""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSizePolicy, QSpinBox,
                               QVBoxLayout, QWidget)

from ..core.sequence import HOLD, LINEAR
from ..core.session import SEQ_IDLE, SEQ_RECORDING, SEQ_REPLAYING
from . import qt_theme as theme
from .qt_widgets import label, paint_glass

#: How close to a keyframe the pointer has to be, in pixels, to grab it.
#: Generous on purpose: these are 9 px dots and the operator may be reaching
#: for one on a rig-room screen rather than a desk monitor.
GRAB_PX = 14

#: Fallback curve colours for controllers whose gas has no theme colour.
FALLBACK_COLORS = ('#f25d38', '#4ecdc4', '#fbbf24', '#60a5fa', '#a78bfa',
                   '#4ade80', '#fb923c', '#f472b6')


def _clock(seconds):
    """``m:ss.s``.  Sequences are minutes long, not hours."""
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _track_color(track, index):
    gas = (track.gas or '').strip()
    color = theme.GAS_COLORS.get(gas)
    if color:
        return color
    return FALLBACK_COLORS[index % len(FALLBACK_COLORS)]


# ---------------------------------------------------------------------- #
#  Timeline strip                                                         #
# ---------------------------------------------------------------------- #
class TimelineStrip(QWidget):
    """Position within the run, and every operator-placed key point.

    Clicking scrubs -- but only when nothing is running.  A click that moved
    the playhead mid-replay would be a command to the rig disguised as a
    glance at the clock.
    """

    scrubbed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._position = 0.0
        self._duration = 0.0
        self._markers = []
        self._state = SEQ_IDLE
        self._cycle = 1
        self._total = 1
        self._holding = False
        self._hold_reason = ''
        self.setFixedHeight(theme.scale(34))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- state ----------------------------------------------------------- #
    def set_state(self, state):
        self._state = state
        self.update()

    def set_markers(self, markers):
        self._markers = sorted(float(value) for value in markers)
        self.update()

    def set_cycle(self, cycle, total):
        """Which pass of a repeated replay is running.  ``total`` 0 = endless."""
        self._cycle = max(1, int(cycle))
        self._total = max(0, int(total))
        self.update()

    def set_hold(self, holding, reason=''):
        """The replay clock is being held because a flow has not arrived."""
        self._holding = bool(holding)
        self._hold_reason = str(reason or '')
        self.update()

    def set_progress(self, position, duration):
        self._position = max(0.0, float(position))
        self._duration = max(0.0, float(duration))
        self.update()

    @property
    def position(self):
        return self._position

    # -- interaction ----------------------------------------------------- #
    def mousePressEvent(self, event):
        if self._state != SEQ_IDLE or self._duration <= 0:
            return
        fraction = event.position().x() / max(1, self.width())
        self.scrubbed.emit(min(1.0, max(0.0, fraction)) * self._duration)

    # -- painting -------------------------------------------------------- #
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = theme.RADIUS_TILE

        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QColor(0, 0, 0, 110))
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(track)

        painter.setClipPath(track)
        if self._state == SEQ_RECORDING:
            # An open-ended run has no proportion to fill, so the strip says
            # "recording" with colour and reports the elapsed time instead of
            # implying a fraction of a length nobody has fixed yet.
            painter.fillRect(rect, QColor(185, 28, 28, 46))
        elif self._duration > 0:
            fraction = min(1.0, self._position / self._duration)
            filled = QRectF(rect.left(), rect.top(),
                            rect.width() * fraction, rect.height())
            if self._state == SEQ_REPLAYING and self._holding:
                # Amber, and clearly not the accent: a held replay is still
                # commanding the setpoints it has already sent, but it is not
                # advancing, and those two look identical on a clock alone.
                color = QColor(theme.WARN)
                color.setAlpha(150)
            else:
                color = QColor(theme.ACCENT if self._state == SEQ_REPLAYING
                               else theme.TEXT_DIM)
                color.setAlpha(150 if self._state == SEQ_REPLAYING else 70)
            painter.fillRect(filled, color)

        if self._duration > 0:
            marker_pen = QPen(QColor(theme.WARN), 1.5)
            for at in self._markers:
                if at > self._duration:
                    continue
                x = rect.left() + rect.width() * (at / self._duration)
                painter.setPen(marker_pen)
                painter.drawLine(QPointF(x, rect.top() + 3),
                                 QPointF(x, rect.bottom() - 3))
            if self._state != SEQ_IDLE or self._position > 0:
                x = rect.left() + rect.width() * min(
                    1.0, self._position / self._duration)
                painter.setPen(QPen(QColor(theme.TEXT_BRIGHT), 2))
                painter.drawLine(QPointF(x, rect.top()),
                                 QPointF(x, rect.bottom()))
        painter.setClipping(False)

        if self._state == SEQ_RECORDING:
            text = f"REC  {_clock(self._position)}"
            color = theme.DANGER_HOVER
        elif self._duration > 0:
            text = f"{_clock(self._position)}  /  {_clock(self._duration)}"
            if self._state == SEQ_REPLAYING and (self._total != 1
                                                 or self._cycle > 1):
                text += (f"   pass {self._cycle}" +
                         (f" / {self._total}" if self._total else " · repeating"))
            if self._state == SEQ_REPLAYING and self._holding:
                text += "   HELD"
            color = (theme.TEXT_BRIGHT if self._state == SEQ_REPLAYING
                     else theme.TEXT_MUTED)
        else:
            text = "no sequence"
            color = theme.TEXT_DIM
        painter.setPen(QColor(color))
        font = painter.font()
        font.setFamilies(theme.FONT_MONO_FAMILIES)
        font.setPointSizeF(theme.font_pt(9))
        painter.setFont(font)
        painter.drawText(rect.adjusted(theme.PAD_MD, 0, -theme.PAD_MD, 0),
                         Qt.AlignmentFlag.AlignVCenter
                         | Qt.AlignmentFlag.AlignLeft, text)
        # While held, the right-hand side says *why* instead of counting key
        # points.  The count is reference; the reason is the thing the operator
        # needs in the second they notice the clock has stopped.
        if self._holding and self._hold_reason:
            note = f"waiting · {self._hold_reason}"
        elif self._markers:
            note = (f"{len(self._markers)} key point"
                    f"{'s' if len(self._markers) != 1 else ''}")
        else:
            note = ''
        if note:
            painter.setPen(QColor(theme.WARN))
            painter.drawText(
                rect.adjusted(theme.PAD_MD, 0, -theme.PAD_MD, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                note)


# ---------------------------------------------------------------------- #
#  Curve editor                                                           #
# ---------------------------------------------------------------------- #
class CurveEditor(pg.PlotWidget):
    """Setpoint against time, one curve per controller, dragged by hand.

    Panning and zooming with the mouse are turned off.  With them on, every
    drag is ambiguous -- is this a keyframe being moved or the view being
    shoved -- and the way that ambiguity resolves in practice is that the
    operator moves a flow they meant to look at.  ``fit()`` and the axis
    handles do the framing instead.

    With no track selected the widget is an overview: every curve at full
    weight on a shared axis, nothing draggable.  Select a track and the rest
    fade back to context so there is no doubt which line a drag will move.
    """

    edited = Signal()
    selection_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent, background=theme.BG_PANEL)
        self._sequence = None
        self._active_key = None
        self._hidden = set()
        self._curves = {}
        self._colors = {}
        self._drag_index = None
        self._read_only = False

        self.setMenuEnabled(False)
        self.setAntialiasing(False)
        plot = self.getPlotItem()
        plot.setLabel('bottom', 'Time', units='s')
        plot.setLabel('left', 'Setpoint', units='SLPM')
        plot.showGrid(x=True, y=True, alpha=0.16)
        plot.getViewBox().setMouseEnabled(False, False)
        plot.hideButtons()
        for axis in ('bottom', 'left'):
            plot.getAxis(axis).setPen(pg.mkPen(theme.BORDER))
            plot.getAxis(axis).setTextPen(pg.mkPen(theme.TEXT_MUTED))

        self._points = pg.ScatterPlotItem(
            size=9, pen=pg.mkPen(theme.BG, width=1),
            brush=pg.mkBrush(theme.TEXT_BRIGHT))
        plot.addItem(self._points)
        self._playhead = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(theme.TEXT_BRIGHT, width=2))
        self._playhead.setVisible(False)
        plot.addItem(self._playhead)
        self._marker_lines = []

    # -- contents -------------------------------------------------------- #
    def set_sequence(self, sequence):
        self._sequence = sequence
        # No active track to begin with, which is the overview: every curve at
        # full weight on one pair of axes.  It is the right thing to land on
        # because the first question about a sequence just opened is what the
        # rig as a whole does, not what one line does.
        self._active_key = None
        self._hidden.clear()
        self._colors = {}
        if sequence is not None:
            for index, track in enumerate(sequence.tracks):
                self._colors[track.key] = _track_color(track, index)
        self._rebuild()
        self.fit()

    def set_active(self, key):
        """Select a track to edit, or ``None`` for the overview."""
        self._active_key = key
        self._drag_index = None
        self._redraw()
        self.fit_active()

    def set_hidden(self, keys):
        self._hidden = set(keys)
        self._redraw()

    def set_read_only(self, read_only):
        """A replay is not the moment to be moving the curve being played."""
        self._read_only = bool(read_only)
        self.setCursor(Qt.CursorShape.ArrowCursor if read_only
                       else Qt.CursorShape.CrossCursor)

    def set_playhead(self, position, visible=True):
        self._playhead.setVisible(bool(visible))
        if visible:
            self._playhead.setPos(float(position))

    @property
    def active_track(self):
        if self._sequence is None or self._active_key is None:
            return None
        return self._sequence.track(self._active_key)

    def color_for(self, key):
        return self._colors.get(key, theme.TEXT)

    # -- framing --------------------------------------------------------- #
    def fit(self):
        """Frame every visible track."""
        if self._sequence is None or not self._sequence.tracks:
            return
        duration = max(self._sequence.duration, 1.0)
        span = max((track.span for track in self._sequence.tracks
                    if track.key not in self._hidden), default=1.0)
        self._set_range(duration, span)

    def fit_active(self):
        """Frame the track being edited, so a small line is not a flat trace.

        Stage air and the pilot differ by two orders of magnitude.  On a
        shared axis the pilot is a line on the floor, and dragging a line on
        the floor is how a 0.4 SLPM pilot becomes a 4 SLPM pilot.
        """
        track = self.active_track
        if track is None:
            self.fit()
            return
        duration = max(self._sequence.duration, 1.0)
        self._set_range(duration, max(track.span, 1.0))

    def _set_range(self, duration, span):
        view = self.getPlotItem().getViewBox()
        view.setXRange(-duration * 0.02, duration * 1.02, padding=0)
        view.setYRange(-span * 0.06, span * 1.12, padding=0)

    # -- drawing --------------------------------------------------------- #
    def _rebuild(self):
        plot = self.getPlotItem()
        for curve in self._curves.values():
            plot.removeItem(curve)
        self._curves = {}
        for line in self._marker_lines:
            plot.removeItem(line)
        self._marker_lines = []
        if self._sequence is None:
            self._points.setData([], [])
            return
        for track in self._sequence.tracks:
            curve = plot.plot([], [])
            self._curves[track.key] = curve
        for at in self._sequence.markers:
            line = pg.InfiniteLine(
                pos=at, angle=90, movable=False,
                pen=pg.mkPen(theme.WARN, width=1,
                             style=Qt.PenStyle.DashLine))
            plot.addItem(line)
            self._marker_lines.append(line)
        # The scatter must stay on top of curves added after it.
        plot.removeItem(self._points)
        plot.addItem(self._points)
        self._redraw()

    def _redraw(self):
        if self._sequence is None:
            self._points.setData([], [])
            return
        # With no track selected every curve is drawn at full weight -- the
        # overview.  Dimming is what says "this one is being edited, the rest
        # are context", and in the overview nothing is being edited.
        overview = self._active_key is None
        for track in self._sequence.tracks:
            curve = self._curves.get(track.key)
            if curve is None:
                continue
            if track.key in self._hidden:
                curve.setData([], [])
                continue
            times, values = track.samples()
            active = track.key == self._active_key
            color = QColor(self.color_for(track.key))
            if not (active or overview):
                color.setAlpha(90)
            pen = pg.mkPen(color, width=2 if active or overview else 1)
            pen.setCosmetic(True)
            curve.setPen(pen)
            curve.setZValue(10 if active else 0)
            curve.setData(times, values)

        track = self.active_track
        if track is None or track.key in self._hidden:
            self._points.setData([], [])
            return
        frames = track.sorted_frames()
        color = QColor(self.color_for(track.key))
        self._points.setData(
            [frame.t for frame in frames],
            [frame.value for frame in frames],
            brush=[pg.mkBrush(color if frame.interp == LINEAR
                              else QColor(theme.TEXT_BRIGHT))
                   for frame in frames],
            pen=pg.mkPen(theme.BG, width=1), size=9)

    def refresh(self):
        """Redraw after the sequence was edited from outside."""
        self._rebuild()

    # -- interaction ----------------------------------------------------- #
    def _data_at(self, position):
        view = self.getPlotItem().getViewBox()
        return view.mapSceneToView(self.mapToScene(position.toPoint()))

    def _hit(self, position):
        """Index of the active track's keyframe under the pointer, or None."""
        track = self.active_track
        if track is None or track.key in self._hidden:
            return None
        point = self._data_at(position)
        view = self.getPlotItem().getViewBox()
        px_x, px_y = view.viewPixelSize()
        if not px_x or not px_y:
            return None
        best, best_distance = None, GRAB_PX
        for index, frame in enumerate(track.sorted_frames()):
            dx = (frame.t - point.x()) / px_x
            dy = (frame.value - point.y()) / px_y
            distance = (dx * dx + dy * dy) ** 0.5
            if distance < best_distance:
                best, best_distance = index, distance
        return best

    def mousePressEvent(self, event):
        if self._read_only or self.active_track is None:
            return
        index = self._hit(event.position())
        if event.button() == Qt.MouseButton.RightButton:
            if index is not None and self.active_track.remove(index):
                self._redraw()
                self.edited.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_index = index
        if index is not None:
            self.selection_changed.emit(index)

    def mouseMoveEvent(self, event):
        if self._read_only or self._drag_index is None:
            return
        track = self.active_track
        if track is None:
            return
        point = self._data_at(event.position())
        # The opening keyframe is pinned to t=0.  It is what the replay
        # primes from, and a run whose first instruction arrives some way in
        # starts from whatever the rig happened to be doing beforehand.
        when = 0.0 if self._drag_index == 0 else point.x()
        track.move(self._drag_index, when, max(0.0, point.y()))
        self._redraw()
        self.edited.emit()

    def mouseReleaseEvent(self, event):
        self._drag_index = None

    def mouseDoubleClickEvent(self, event):
        if self._read_only:
            return
        track = self.active_track
        if track is None:
            return
        point = self._data_at(event.position())
        if point.x() <= 0:
            return
        index = track.add(point.x(), max(0.0, point.y()))
        self._redraw()
        self.selection_changed.emit(index)
        self.edited.emit()


# ---------------------------------------------------------------------- #
#  Panel                                                                  #
# ---------------------------------------------------------------------- #
class SequencePanel(QWidget):
    """Transport, track list and curve editor over a :class:`FlowSession`."""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._selected_index = None
        self._building = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.PAD_MD, theme.PAD_MD,
                                 theme.PAD_MD, theme.PAD_MD)
        outer.setSpacing(theme.PAD_SM)

        outer.addLayout(self._build_transport())
        self.timeline = TimelineStrip()
        self.timeline.scrubbed.connect(self._on_scrub)
        outer.addWidget(self.timeline)

        body = QHBoxLayout()
        body.setSpacing(theme.PAD_MD)
        body.addWidget(self._build_track_list())
        editor_column = QVBoxLayout()
        editor_column.setSpacing(theme.PAD_SM)
        self.editor = CurveEditor()
        self.editor.edited.connect(self._on_edited)
        self.editor.selection_changed.connect(self._on_frame_selected)
        editor_column.addWidget(self.editor, 1)
        editor_column.addLayout(self._build_frame_controls())
        body.addLayout(editor_column, 1)
        outer.addLayout(body, 1)

        session.sequence_state_changed.connect(self._on_state)
        session.sequence_progress.connect(self._on_progress)
        session.sequence_changed.connect(self._on_sequence)
        session.sequence_keyframe_added.connect(self._on_marker)
        session.sequence_saved.connect(self._on_saved)
        session.sequence_cycle.connect(self.timeline.set_cycle)
        session.sequence_hold.connect(self.timeline.set_hold)
        session.connection_changed.connect(lambda _on: self._sync())
        session.monitoring_changed.connect(lambda _on: self._sync())

        self._on_sequence(session.sequence)
        self._sync()

    # -- construction ---------------------------------------------------- #
    def _build_transport(self):
        bar = QHBoxLayout()
        bar.setSpacing(theme.PAD_SM)

        self.record_btn = QPushButton('● Record')
        self.record_btn.setProperty('variant', 'danger')
        self.record_btn.clicked.connect(self._on_record)
        bar.addWidget(self.record_btn)

        # No "add key point" button.  A recording already lands a keyframe on
        # every track each time a setpoint changes, so an operator-placed anchor
        # at the value the track is already holding adds nothing to the curve.
        self.play_btn = QPushButton('▶ Replay')
        self.play_btn.setProperty('variant', 'accent')
        self.play_btn.clicked.connect(self._on_play)
        bar.addWidget(self.play_btn)

        bar.addWidget(label('REPEAT', color=theme.TEXT_DIM, size=7, bold=True))
        self.repeat_spin = QSpinBox()
        # 0 is the special value because it is the only one Qt will label, and
        # "until stopped" is the setting that most needs spelling out rather
        # than being inferred from a number.
        self.repeat_spin.setRange(0, 999)
        self.repeat_spin.setValue(1)
        self.repeat_spin.setPrefix('× ')
        self.repeat_spin.setSpecialValueText('until stopped')
        self.repeat_spin.setFixedWidth(theme.scale(112))
        self.repeat_spin.setToolTip(
            'How many passes to make. Each pass returns to the opening '
            'setpoints first, so a repeat drives the rig from the closing '
            'state back to the starting one.')
        bar.addWidget(self.repeat_spin)

        bar.addSpacing(theme.PAD_MD)
        self.settle_check = QCheckBox('Hold if flows lag')
        self.settle_check.setChecked(self.session.settle_enabled)
        self.settle_check.setToolTip(
            'Compare each line against what it was told, and hold the clock '
            'for every controller while any one of them is out of tolerance, '
            'so the next transition is delayed rather than laid on top of one '
            'that has not finished.')
        self.settle_check.toggled.connect(self._on_settle_toggled)
        bar.addWidget(self.settle_check)

        self.settle_spin = QSpinBox()
        self.settle_spin.setRange(1, 50)
        self.settle_spin.setValue(
            max(1, round(self.session.settle_tolerance * 100)))
        self.settle_spin.setSuffix(' %')
        self.settle_spin.setFixedWidth(theme.scale(78))
        self.settle_spin.setToolTip(
            'How far a line may sit from its setpoint before the clock is '
            'held, as a percentage of that line’s own largest recorded value.')
        self.settle_spin.valueChanged.connect(self._on_settle_toggled)
        self.settle_spin.setEnabled(self.session.settle_enabled)
        bar.addWidget(self.settle_spin)

        bar.addSpacing(theme.PAD_MD)
        self.load_btn = QPushButton('Open…')
        self.load_btn.setProperty('variant', 'quiet')
        self.load_btn.clicked.connect(self._on_load)
        bar.addWidget(self.load_btn)
        self.save_btn = QPushButton('Save As…')
        self.save_btn.setProperty('variant', 'quiet')
        self.save_btn.clicked.connect(self._on_save)
        bar.addWidget(self.save_btn)
        self.clear_btn = QPushButton('Clear')
        self.clear_btn.setProperty('variant', 'quiet')
        self.clear_btn.setToolTip(
            'Put this sequence down, so the next recording starts from an '
            'empty timeline instead of replacing it. Files already saved to '
            'disk are untouched.')
        self.clear_btn.clicked.connect(self._on_clear)
        bar.addWidget(self.clear_btn)

        bar.addStretch(1)
        self.name_label = label('No sequence loaded', color=theme.TEXT_DIM,
                                size=9, monospace=True)
        bar.addWidget(self.name_label)
        return bar

    def _build_track_list(self):
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.PAD_XS)
        column.addWidget(label('TRACKS', color=theme.TEXT_DIM, size=7, bold=True))
        self.track_list = QListWidget()
        self.track_list.setFixedWidth(theme.scale(190))
        # No sheet of its own: every list in the app is dressed once, in
        # qt_theme, so they cannot drift apart.
        self.track_list.currentRowChanged.connect(self._on_track_row)
        self.track_list.itemChanged.connect(self._on_track_visibility)
        column.addWidget(self.track_list, 1)

        # How fast each line may move is not set here.  It is a property of the
        # controller and the plumbing behind it rather than of one recording, so
        # it is declared on the controller's own card in the operation tab and
        # paces every setpoint that line is given -- typed, ignition, or
        # replayed from here.
        hint = QLabel('Pick a track to edit it · drag a point to move it · '
                      'double-click to add · right-click to delete')
        hint.setObjectName('Hint')
        hint.setWordWrap(True)
        column.addWidget(hint)
        return holder

    def _build_frame_controls(self):
        bar = QHBoxLayout()
        bar.setSpacing(theme.PAD_SM)
        self.frame_label = label('No key point selected', color=theme.TEXT_DIM,
                                 size=9, monospace=True)
        bar.addWidget(self.frame_label)
        bar.addStretch(1)

        bar.addWidget(label('TRANSITION', color=theme.TEXT_DIM, size=7,
                            bold=True))
        self.interp_combo = QComboBox()
        self.interp_combo.addItems(['Hold (step)', 'Ramp (linear)'])
        self.interp_combo.setFixedWidth(theme.scale(128))
        self.interp_combo.setEnabled(False)
        self.interp_combo.activated.connect(self._on_interp)
        bar.addWidget(self.interp_combo)

        fit = QPushButton('Fit')
        fit.setProperty('variant', 'quiet')
        fit.setProperty('density', 'compact')
        fit.clicked.connect(self.editor.fit)
        bar.addWidget(fit)
        return bar

    # -- session signals ------------------------------------------------- #
    def _on_sequence(self, sequence):
        self.editor.set_sequence(sequence)
        self._selected_index = None
        self._populate_tracks(sequence)
        self.timeline.set_markers(sequence.markers if sequence else [])
        self.timeline.set_progress(0.0, sequence.duration if sequence else 0.0)
        self._describe_sequence()
        self._describe_frame()
        self._sync()

    def _describe_sequence(self):
        """Say what this panel is holding, or what it is doing instead.

        While a recording is running there is no sequence yet -- it is built
        when the recording stops -- but "No sequence loaded" is the wrong
        thing to read at the moment setpoints are being captured, because it
        describes the panel rather than the rig.
        """
        if self.session.sequence_state == SEQ_RECORDING:
            self.name_label.setText('Recording — every setpoint sent from '
                                    'now is being captured')
            return
        sequence = self.session.sequence
        if sequence is None:
            self.name_label.setText('No sequence loaded')
            return
        self.name_label.setText(
            f"{sequence.name}   {sequence.duration:.1f} s   "
            f"{len(sequence.tracks)} track"
            f"{'s' if len(sequence.tracks) != 1 else ''}")

    def _on_state(self, state):
        self.timeline.set_state(state)
        if state != SEQ_REPLAYING:
            # Otherwise the strip keeps reading "pass 7 / 10" after the run it
            # was counting has stopped, or stays amber because the run was
            # stopped by hand while a line was still lagging.
            self.timeline.set_cycle(1, 1)
            self.timeline.set_hold(False, '')
        self.editor.set_read_only(state != SEQ_IDLE)
        self.editor.set_playhead(0.0, visible=state == SEQ_REPLAYING)
        self._describe_sequence()
        self._sync()

    def _on_progress(self, position, duration):
        self.timeline.set_progress(position, duration)
        if self.session.sequence_state == SEQ_REPLAYING:
            self.editor.set_playhead(position)

    def _on_marker(self, at):
        self.timeline.set_markers(list(self.timeline._markers) + [at])

    def _on_saved(self, path):
        self.name_label.setToolTip(str(path))

    # -- transport ------------------------------------------------------- #
    def _on_record(self):
        if self.session.sequence_state == SEQ_RECORDING:
            self.session.stop_recording()
        else:
            self.session.start_recording()

    def _on_play(self):
        """Replay for as many passes as the transport asks for, or stop."""
        if self.session.sequence_state == SEQ_REPLAYING:
            self.session.stop_replay()
            return
        self.request_replay(self.repeat_spin.value())

    def request_replay(self, repeats=1):
        """Start a replay, or say what is in the way of starting one.

        The monitor gets its own question rather than a refusal.  Opening a
        sequence and pressing Replay is an unambiguous instruction, and the only
        thing missing is the loop that carries setpoints to the controllers --
        so offer to start it here instead of sending the operator to another tab
        to find out what "connect the controllers and start the monitor" meant.
        Every other obstacle is the session's to report: it knows about the zero
        lock, the ignition sequence and unassigned tracks, and its messages say
        which one applied.

        Public because the operation tab's quick-play list needs exactly this
        and would otherwise grow its own copy of the prompt -- two dialogs
        wording the same prerequisite differently, which is how an operator ends
        up unsure whether they are the same question.
        """
        if not self.session.is_monitoring and self.session.controllers_connected:
            name = getattr(self.session.sequence, 'name', 'this sequence')
            answer = QMessageBox.question(
                self, 'Start monitoring',
                f"The live monitor is not running, so nothing is being written "
                f"to the controllers yet.\n\nStart monitoring and replay "
                f"'{name}'?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return False
            if not self.session.start_monitoring():
                return False
        return self.session.start_replay(repeats=repeats)

    def _on_load(self):
        directory = str(self.session.sequence_dir)
        Path(directory).mkdir(parents=True, exist_ok=True)
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Open sequence', directory,
            'Flow controller sequences (*.fcseq.json);;All files (*)')
        if path:
            self.session.load_sequence(Path(path))

    def _on_save(self):
        if self.session.sequence is None:
            return
        directory = str(self.session.sequence_dir /
                        f"{self.session.sequence.name}.fcseq.json")
        Path(self.session.sequence_dir).mkdir(parents=True, exist_ok=True)
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Save sequence', directory,
            'Flow controller sequences (*.fcseq.json);;All files (*)')
        if path:
            self.session.save_sequence(Path(path))

    def _on_clear(self):
        """Put the loaded sequence down so a fresh recording has room.

        Asked rather than done, because the panel is the only place an edited
        sequence exists until it is saved and the curves on screen may be worth
        several minutes of dragging.  The prompt says what survives -- anything
        already written to disk -- so the answer does not depend on remembering
        whether Save As was pressed.
        """
        sequence = self.session.sequence
        if sequence is None:
            return
        answer = QMessageBox.question(
            self, 'Clear sequence',
            f"Clear '{sequence.name}' from the panel?\n\n"
            "The timeline is emptied so the next recording starts fresh. Any "
            "copy already saved to disk is untouched and can be opened again; "
            "edits made here since it was saved are not kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.session.set_sequence(None)

    def _on_scrub(self, position):
        self.editor.set_playhead(position, visible=True)

    def _on_settle_toggled(self, _value=None):
        """Push the discrepancy policy at the session.

        Both widgets land here, and the session is the one place the policy
        lives -- the gate itself is rebuilt per replay, so setting this mid-run
        changes the run in progress as well as the next one, which is what an
        operator watching a line lag actually wants.
        """
        enabled = self.settle_check.isChecked()
        self.settle_spin.setEnabled(enabled)
        self.session.set_settle_policy(
            enabled=enabled, tolerance=self.settle_spin.value() / 100.0)

    # -- tracks ---------------------------------------------------------- #
    def _populate_tracks(self, sequence):
        self._building = True
        try:
            self.track_list.clear()
            if sequence is None:
                return
            overview = QListWidgetItem('All tracks  (overview)')
            # No checkbox: it is not a track that can be hidden, it is the view
            # you get when no track is singled out.
            overview.setData(Qt.ItemDataRole.UserRole, None)
            overview.setForeground(QColor(theme.TEXT_BRIGHT))
            overview.setToolTip(
                'Every controller overlaid on one pair of axes. Pick a track '
                'below to edit its curve.')
            self.track_list.addItem(overview)
            for index, track in enumerate(sequence.tracks):
                item = QListWidgetItem(track.label)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                item.setForeground(QColor(_track_color(track, index)))
                item.setData(Qt.ItemDataRole.UserRole, track.key)
                unit = track.unit or '--'
                item.setToolTip(f"{track.label} · recorded on unit {unit}")
                self.track_list.addItem(item)
            self.track_list.setCurrentRow(0)
        finally:
            self._building = False

    def _on_track_row(self, row):
        if self._building or row < 0:
            return
        item = self.track_list.item(row)
        if item is None:
            return
        self.editor.set_active(item.data(Qt.ItemDataRole.UserRole))
        self._selected_index = None
        self._describe_frame()

    def _on_track_visibility(self, _item):
        if self._building:
            return
        hidden = set()
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            key = item.data(Qt.ItemDataRole.UserRole)
            # The overview row carries no key and no checkbox; Qt still reports
            # an unchecked state for it, which would put ``None`` in the hidden
            # set and read as a track nobody has.
            if key is None:
                continue
            if item.checkState() == Qt.CheckState.Unchecked:
                hidden.add(key)
        self.editor.set_hidden(hidden)

    def _on_frame_selected(self, index):
        self._selected_index = index
        self._describe_frame()

    def _on_interp(self, choice):
        track = self.editor.active_track
        if track is None or self._selected_index is None:
            return
        track.set_interp(self._selected_index, LINEAR if choice else HOLD)
        self.editor.refresh()
        self._on_edited()
        self._describe_frame()

    def _describe_frame(self):
        track = self.editor.active_track
        frames = track.sorted_frames() if track is not None else []
        if track is None or self._selected_index is None \
                or not 0 <= self._selected_index < len(frames):
            self.frame_label.setText('No key point selected')
            self.interp_combo.setEnabled(False)
            return
        frame = frames[self._selected_index]
        pinned = '  (pinned to the start)' if self._selected_index == 0 else ''
        self.frame_label.setText(
            f"{track.label}   t = {frame.t:.2f} s   "
            f"{frame.value:.3f} SLPM{pinned}")
        self.interp_combo.setEnabled(self.session.sequence_state == SEQ_IDLE)
        self.interp_combo.setCurrentIndex(1 if frame.interp == LINEAR else 0)

    def _on_edited(self):
        sequence = self.session.sequence
        if sequence is None:
            return
        self.timeline.set_progress(self.timeline.position, sequence.duration)
        self.name_label.setText(
            f"{sequence.name} *   {sequence.duration:.1f} s   "
            f"{len(sequence.tracks)} track"
            f"{'s' if len(sequence.tracks) != 1 else ''}")
        self._describe_frame()

    # -- enabling -------------------------------------------------------- #
    def _sync(self):
        state = self.session.sequence_state
        recording = state == SEQ_RECORDING
        replaying = state == SEQ_REPLAYING
        idle = state == SEQ_IDLE
        has_sequence = self.session.sequence is not None

        self.record_btn.setText('■ Stop Recording' if recording else '● Record')
        self.record_btn.setEnabled(recording or (idle and self.session.is_monitoring))
        self.record_btn.setToolTip(
            '' if self.session.is_monitoring or recording else
            'Start the live monitor before recording — otherwise nothing is '
            'being written to the controllers to record.')
        self.play_btn.setText('■ Stop Replay' if replaying else '▶ Replay')
        # A sequence in hand is the whole condition.  Requiring the monitor here
        # as well is what left an opened sequence with a dead Replay button and
        # nothing on screen to say why; the prerequisites are better handled
        # when the button is pressed, where they can be offered or explained.
        self.play_btn.setEnabled(replaying or (idle and has_sequence))
        self.play_btn.setToolTip(
            'Stop the run and leave the controllers where they are.'
            if replaying else
            'Record or open a sequence first.' if not has_sequence else
            'Connect the controllers first — a replay drives them through the '
            'recorded setpoints.' if not self.session.controllers_connected else
            'Drive the rig through this sequence from the top. The live monitor '
            'is not running yet; you will be asked to start it, because a '
            'replay writes its setpoints through the monitor loop.'
            if not self.session.is_monitoring else
            'Drive the rig through this sequence from the top.')
        self.load_btn.setEnabled(idle)
        self.save_btn.setEnabled(idle and has_sequence)
        self.clear_btn.setEnabled(idle and has_sequence)
        self.repeat_spin.setEnabled(idle)
        self.track_list.setEnabled(not recording)
        self._describe_frame()

    # -- painting -------------------------------------------------------- #
    def paintEvent(self, _event):
        paint_glass(self, radius=theme.RADIUS_CARD)
