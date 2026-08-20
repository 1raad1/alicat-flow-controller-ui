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
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from ..core.sequence import HOLD, LINEAR, SMOOTH
from ..core.session import SEQ_IDLE, SEQ_RECORDING, SEQ_REPLAYING
from ..domain.roles import AIR_KEYS
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


def _is_air_track(track):
    """Whether ``track`` belongs on the overview's air-flow axis."""
    return (track.key in AIR_KEYS
            or (track.gas or '').strip().casefold() == 'air')


class KeyPointDialog(QDialog):
    """Numerical editor for one automation-style key point.

    The graph remains the quick way to shape a run; this is the precise path
    for values that should land at an exact time or controller setpoint.
    """

    def __init__(self, track, *, index=None, when=0.0, value=0.0,
                 interp=HOLD, parent=None):
        super().__init__(parent)
        editing = index is not None
        self.setWindowTitle('Edit key point' if editing else 'Add key point')
        self.setModal(True)
        self.setMinimumWidth(theme.scale(340))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.PAD_LG, theme.PAD_LG,
                                 theme.PAD_LG, theme.PAD_LG)
        outer.setSpacing(theme.PAD_MD)
        heading = label(track.label, color=theme.TEXT_BRIGHT, size=11,
                        bold=True)
        outer.addWidget(heading)

        form = QFormLayout()
        form.setSpacing(theme.PAD_SM)
        self.time_spin = QDoubleSpinBox()
        self.time_spin.setObjectName('KeyPointTime')
        self.time_spin.setDecimals(3)
        self.time_spin.setSingleStep(0.1)
        self.time_spin.setSuffix(' s')
        self.time_spin.setKeyboardTracking(False)

        frames = track.sorted_frames()
        if editing and 0 <= index < len(frames):
            minimum = frames[index - 1].t + 0.001 if index > 0 else 0.0
            maximum = (frames[index + 1].t - 0.001
                       if index + 1 < len(frames) else 1_000_000_000.0)
            if index == 0:
                self.time_spin.setEnabled(False)
                self.time_spin.setToolTip(
                    'The opening point stays at 0 s because replay starts '
                    'from its value.')
        else:
            # A point at zero would replace the structural opening point.
            minimum, maximum = 0.001, 1_000_000_000.0
        self.time_spin.setRange(minimum, max(minimum, maximum))
        self.time_spin.setValue(float(when))
        form.addRow('Time', self.time_spin)

        self.value_spin = QDoubleSpinBox()
        self.value_spin.setObjectName('KeyPointValue')
        self.value_spin.setDecimals(6)
        self.value_spin.setSingleStep(0.1)
        self.value_spin.setRange(0.0, 1_000_000_000.0)
        self.value_spin.setSuffix(' SLPM')
        self.value_spin.setKeyboardTracking(False)
        self.value_spin.setValue(float(value))
        form.addRow('Setpoint', self.value_spin)

        self.interp_combo = QComboBox()
        self.interp_combo.setObjectName('KeyPointTransition')
        self.interp_combo.addItem('Hold (step)', HOLD)
        self.interp_combo.addItem('Ramp (linear)', LINEAR)
        self.interp_combo.addItem('Smooth (eased)', SMOOTH)
        selected = self.interp_combo.findData(interp)
        self.interp_combo.setCurrentIndex(max(0, selected))
        self.interp_combo.setToolTip(
            'How this point leads into the following point.')
        form.addRow('Transition after', self.interp_combo)
        outer.addLayout(form)

        note = QLabel('Time is kept between the neighbouring key points. '
                      'Setpoints cannot be negative.')
        note.setObjectName('Hint')
        note.setWordWrap(True)
        outer.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @property
    def values(self):
        return (self.time_spin.value(), self.value_spin.value(),
                self.interp_combo.currentData())


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
    weight and nothing draggable.  Fuel uses the left scale and air the right,
    because the air span is usually much larger and would otherwise flatten the
    fuel traces.  Select a track and the editor returns to one dedicated scale;
    the rest fade back to context so there is no doubt which line a drag moves.
    """

    edited = Signal()
    selection_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent, background=theme.BG_PANEL)
        self._sequence = None
        self._active_key = None
        self._hidden = set()
        self._curves = {}
        self._air_curve_keys = set()
        self._colors = {}
        self._drag_index = None
        self._selected_index = None
        self._selected_indices = set()
        self._selection_anchor = None
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

        # Pyqtgraph implements a second y-axis with a linked ViewBox.  It is
        # enabled only for the all-track overview; individual-track editing
        # keeps the original single coordinate system for hit testing and drag.
        self._air_view = pg.ViewBox(enableMenu=False)
        self._air_view.setMouseEnabled(False, False)
        plot.scene().addItem(self._air_view)
        plot.getAxis('right').linkToView(self._air_view)
        self._air_view.setXLink(plot.getViewBox())
        plot.getViewBox().sigResized.connect(self._sync_air_view)
        right = plot.getAxis('right')
        right.setPen(pg.mkPen(theme.BORDER))
        right.setTextPen(pg.mkPen(theme.GAS_COLORS.get('Air', theme.TEXT_MUTED)))
        plot.hideAxis('right')
        self._sync_air_view()

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
        # full weight, with fuel and air scaled separately.  It is right to land on
        # because the first question about a sequence just opened is what the
        # rig as a whole does, not what one line does.
        self._active_key = None
        self._selected_index = None
        self._selected_indices.clear()
        self._selection_anchor = None
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
        self._selected_index = None
        self._selected_indices.clear()
        self._selection_anchor = None
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

    @property
    def selected_indices(self):
        """The active track's selected points, in timeline order."""
        return tuple(sorted(self._selected_indices))

    # -- framing --------------------------------------------------------- #
    def fit(self):
        """Frame every visible track."""
        if self._sequence is None or not self._sequence.tracks:
            return
        duration = max(self._sequence.duration, 1.0)
        visible = [track for track in self._sequence.tracks
                   if track.key not in self._hidden]
        if self._active_key is None and any(_is_air_track(track)
                                            for track in visible):
            fuel_span = max((track.span for track in visible
                             if not _is_air_track(track)), default=1.0)
            air_span = max((track.span for track in visible
                            if _is_air_track(track)), default=1.0)
            self._set_overview_ranges(duration, max(fuel_span, 1.0),
                                      max(air_span, 1.0))
        else:
            span = max((track.span for track in visible), default=1.0)
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

    def _set_overview_ranges(self, duration, fuel_span, air_span):
        """Frame fuel and air independently while sharing one time range."""
        view = self.getPlotItem().getViewBox()
        view.setXRange(-duration * 0.02, duration * 1.02, padding=0)
        view.setYRange(-fuel_span * 0.06, fuel_span * 1.12, padding=0)
        self._air_view.setYRange(-air_span * 0.06, air_span * 1.12, padding=0)

    def _sync_air_view(self):
        """Keep the secondary plotting area exactly over the main ViewBox."""
        plot = self.getPlotItem()
        primary = plot.getViewBox()
        self._air_view.setGeometry(primary.sceneBoundingRect())
        self._air_view.linkedViewChanged(primary, self._air_view.XAxis)

    def _configure_curve_axes(self, overview):
        """Put air curves on the secondary axis only in the grouped view."""
        plot = self.getPlotItem()
        wanted_air = {
            track.key for track in (self._sequence.tracks
                                    if self._sequence is not None else ())
            if overview and _is_air_track(track)
        }
        for key, curve in self._curves.items():
            on_air = key in self._air_curve_keys
            should_be_air = key in wanted_air
            if on_air == should_be_air:
                continue
            if should_be_air:
                plot.removeItem(curve)
                self._air_view.addItem(curve)
                self._air_curve_keys.add(key)
            else:
                self._air_view.removeItem(curve)
                plot.addItem(curve)
                self._air_curve_keys.discard(key)

        visible_air = bool(wanted_air - self._hidden)
        if visible_air:
            plot.showAxis('right')
            plot.setLabel('left', 'Fuel setpoint', units='SLPM')
            plot.setLabel('right', 'Air setpoint', units='SLPM')
        else:
            plot.hideAxis('right')
            plot.setLabel('left', 'Setpoint', units='SLPM')
        self._sync_air_view()

    # -- drawing --------------------------------------------------------- #
    def _rebuild(self):
        plot = self.getPlotItem()
        for key, curve in self._curves.items():
            if key in self._air_curve_keys:
                self._air_view.removeItem(curve)
            else:
                plot.removeItem(curve)
        self._curves = {}
        self._air_curve_keys.clear()
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
            self._configure_curve_axes(False)
            self._points.setData([], [])
            return
        # With no track selected every curve is drawn at full weight -- the
        # overview.  Dimming is what says "this one is being edited, the rest
        # are context", and in the overview nothing is being edited.
        overview = self._active_key is None
        self._configure_curve_axes(overview)
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
        selected = self._selected_indices
        self._points.setData(
            [frame.t for frame in frames],
            [frame.value for frame in frames],
            brush=[pg.mkBrush(
                color if frame.interp == LINEAR else
                QColor(theme.ACCENT) if frame.interp == SMOOTH else
                QColor(theme.TEXT_BRIGHT))
                   for frame in frames],
            pen=[pg.mkPen(theme.WARN if index in selected else theme.BG,
                          width=2 if index in selected else 1)
                 for index, _frame in enumerate(frames)],
            size=[12 if index in selected else 9
                  for index, _frame in enumerate(frames)])

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
            point = self._data_at(event.position())
            self._show_context_menu(index, point,
                                    event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if index is not None:
            modifiers = event.modifiers()
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._select_range(index)
                self._drag_index = None
            elif modifiers & Qt.KeyboardModifier.ControlModifier:
                self._toggle_select(index)
                self._drag_index = None
            else:
                self._select(index)
                self._drag_index = index
        else:
            self._clear_selection()

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
        index = self._hit(event.position())
        if index is not None:
            self._select(index)
            self._edit_point(index)
            return
        point = self._data_at(event.position())
        if point.x() <= 0:
            return
        index = track.add(point.x(), max(0.0, point.y()))
        self._select(index)
        self.edited.emit()

    def keyPressEvent(self, event):
        """Mirror timeline editors: Delete removes the selected point."""
        if (not self._read_only
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and self._selected_index is not None):
            self._delete_point(self._selected_index)
            return
        if (not self._read_only
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and self._selected_index is not None):
            self._edit_point(self._selected_index)
            return
        super().keyPressEvent(event)

    def _select(self, index):
        self._selected_indices = {index}
        self._selected_index = index
        self._selection_anchor = index
        self._redraw()
        self.selection_changed.emit(index)

    def _select_range(self, index):
        """Select every point between the anchor and ``index``."""
        anchor = (self._selection_anchor if self._selection_anchor is not None
                  else self._selected_index)
        if anchor is None:
            anchor = index
        low, high = sorted((anchor, index))
        self._selected_indices = set(range(low, high + 1))
        self._selected_index = index
        self._redraw()
        self.selection_changed.emit(index)

    def _toggle_select(self, index):
        """Add or remove one point without losing the rest of the group."""
        if index in self._selected_indices:
            self._selected_indices.remove(index)
        else:
            self._selected_indices.add(index)
        self._selection_anchor = index
        self._selected_index = (index if index in self._selected_indices else
                                (max(self._selected_indices)
                                 if self._selected_indices else None))
        self._redraw()
        self.selection_changed.emit(
            self._selected_index if self._selected_index is not None else -1)

    def _clear_selection(self):
        self._selected_indices.clear()
        self._selected_index = None
        self._selection_anchor = None
        self._redraw()
        self.selection_changed.emit(-1)

    def select_indices(self, indices):
        """Select a point group programmatically (also useful for shortcuts)."""
        track = self.active_track
        count = len(track.sorted_frames()) if track is not None else 0
        selected = {int(index) for index in indices
                    if 0 <= int(index) < count}
        self._selected_indices = selected
        self._selected_index = max(selected) if selected else None
        self._selection_anchor = self._selected_index
        self._redraw()
        self.selection_changed.emit(
            self._selected_index if self._selected_index is not None else -1)

    def _show_context_menu(self, index, point, global_position):
        """Show deliberate point actions instead of deleting on right-click."""
        track = self.active_track
        if track is None:
            return
        menu = QMenu(self)
        if index is None:
            add_action = menu.addAction('Add key point here…')
            chosen = menu.exec(global_position)
            if chosen == add_action:
                self._edit_point(None, when=max(0.001, point.x()),
                                 value=max(0.0, point.y()))
            return

        frames = track.sorted_frames()
        if not 0 <= index < len(frames):
            return
        frame = frames[index]
        self._select(index)
        summary = menu.addAction(
            f'{frame.t:.3f} s   ·   {frame.value:.6g} SLPM')
        summary.setEnabled(False)
        edit_action = menu.addAction('Edit time and value…')

        transition = menu.addMenu('Transition after point')
        hold_action = transition.addAction('Hold (step)')
        linear_action = transition.addAction('Ramp (linear)')
        smooth_action = transition.addAction('Smooth (eased)')
        for action, kind in ((hold_action, HOLD), (linear_action, LINEAR),
                             (smooth_action, SMOOTH)):
            action.setCheckable(True)
            action.setChecked(frame.interp == kind)

        menu.addSeparator()
        delete_action = menu.addAction('Delete key point')
        delete_action.setEnabled(index > 0)
        if index == 0:
            delete_action.setText('Delete key point  (opening point is pinned)')

        chosen = menu.exec(global_position)
        if chosen == edit_action:
            self._edit_point(index)
        elif chosen == hold_action:
            self._set_point_interp(index, HOLD)
        elif chosen == linear_action:
            self._set_point_interp(index, LINEAR)
        elif chosen == smooth_action:
            self._set_point_interp(index, SMOOTH)
        elif chosen == delete_action:
            self._delete_point(index)

    def _edit_point(self, index, *, when=None, value=None):
        """Open the numerical editor for an existing or new point."""
        track = self.active_track
        if track is None:
            return
        frames = track.sorted_frames()
        if index is not None:
            if not 0 <= index < len(frames):
                return
            frame = frames[index]
            when, value, interp = frame.t, frame.value, frame.interp
        else:
            interp = track._interp_at(float(when))
        dialog = KeyPointDialog(track, index=index, when=when, value=value,
                                interp=interp, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        when, value, interp = dialog.values
        if index is None:
            index = track.add(when, value, interp)
        else:
            track.move(index, when, value)
            track.set_interp(index, interp)
        self._select(index)
        self.edited.emit()

    def _set_point_interp(self, index, interp):
        track = self.active_track
        if track is not None and track.set_interp(index, interp):
            self._select(index)
            self.edited.emit()

    def set_selected_interp(self, interp, *, between_only=False):
        """Apply a transition to the selected group.

        For smoothing, ``between_only`` keeps the transition leaving the final
        selected point unchanged.  The edit then affects only the span visibly
        enclosed by the selected group.
        """
        track = self.active_track
        selected = set(self._selected_indices)
        if track is None or not selected:
            return False
        targets = selected
        if between_only:
            targets = {index for index in selected if index + 1 in selected}
        if not targets:
            return False
        track.set_interps(targets, interp)
        self._redraw()
        self.edited.emit()
        return True

    def smooth_selected(self):
        """Ease the transitions contained inside the selected point group."""
        return self.set_selected_interp(SMOOTH, between_only=True)

    def _delete_point(self, index):
        track = self.active_track
        if track is None or not track.remove(index):
            return
        self._selected_index = None
        self._selected_indices.clear()
        self._selection_anchor = None
        self._redraw()
        self.selection_changed.emit(-1)
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
        editor_column.addLayout(self._build_edit_controls())
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
                      'double-click to add or edit · right-click for exact '
                      'values and actions')
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
        self.interp_combo.addItem('Hold (step)', HOLD)
        self.interp_combo.addItem('Ramp (linear)', LINEAR)
        self.interp_combo.addItem('Smooth (eased)', SMOOTH)
        self.interp_combo.setFixedWidth(theme.scale(142))
        self.interp_combo.setEnabled(False)
        self.interp_combo.activated.connect(self._on_interp)
        bar.addWidget(self.interp_combo)

        fit = QPushButton('Fit')
        fit.setProperty('variant', 'quiet')
        fit.setProperty('density', 'compact')
        fit.clicked.connect(self.editor.fit)
        bar.addWidget(fit)
        return bar

    def _build_edit_controls(self):
        """Whole-timeline timing and smoothing transforms."""
        bar = QHBoxLayout()
        bar.setSpacing(theme.PAD_SM)

        bar.addWidget(label('SEQUENCE SPEED', color=theme.TEXT_DIM, size=7,
                            bold=True))
        self.slower_btn = QPushButton('− Slower')
        self.slower_btn.setProperty('variant', 'quiet')
        self.slower_btn.setProperty('density', 'compact')
        self.slower_btn.setToolTip(
            'Expand every track and marker by 1.2×. Press Speed up once to '
            'reverse this exact timing change.')
        self.slower_btn.clicked.connect(
            lambda: self._on_speed(1.0 / 1.2))
        bar.addWidget(self.slower_btn)

        self.faster_btn = QPushButton('+ Speed up')
        self.faster_btn.setProperty('variant', 'quiet')
        self.faster_btn.setProperty('density', 'compact')
        self.faster_btn.setToolTip(
            'Compress every track and marker by 1.2×. Press Slower once to '
            'reverse this exact timing change.')
        self.faster_btn.clicked.connect(lambda: self._on_speed(1.2))
        bar.addWidget(self.faster_btn)

        bar.addSpacing(theme.PAD_MD)
        self.smooth_selected_btn = QPushButton('Smooth selected')
        self.smooth_selected_btn.setProperty('variant', 'quiet')
        self.smooth_selected_btn.setProperty('density', 'compact')
        self.smooth_selected_btn.setToolTip(
            'Ease the transitions between the selected points. Shift-click '
            'selects a continuous range; Ctrl-click adds or removes points.')
        self.smooth_selected_btn.clicked.connect(self._on_smooth_selected)
        bar.addWidget(self.smooth_selected_btn)

        self.smooth_all_btn = QPushButton('Smooth whole sequence')
        self.smooth_all_btn.setProperty('variant', 'quiet')
        self.smooth_all_btn.setProperty('density', 'compact')
        self.smooth_all_btn.setToolTip(
            'Ease every transition on every track while preserving all key '
            'point times and values.')
        self.smooth_all_btn.clicked.connect(self._on_smooth_all)
        bar.addWidget(self.smooth_all_btn)
        bar.addStretch(1)
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
                'Every controller overlaid against time. Fuel uses the left '
                'scale and air the right. Pick a track below to edit its curve.')
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
        self._selected_index = index if index >= 0 else None
        self._describe_frame()

    def _on_interp(self, choice):
        track = self.editor.active_track
        if track is None or self._selected_index is None:
            return
        interp = self.interp_combo.itemData(choice)
        self.editor.set_selected_interp(interp)

    def _on_speed(self, multiplier):
        """Retime the complete sequence by one reversible speed step."""
        sequence = self.session.sequence
        if (sequence is None or self.session.sequence_state != SEQ_IDLE
                or not sequence.scale_speed(multiplier)):
            return
        self.timeline.set_markers(sequence.markers)
        self.timeline.set_progress(
            self.timeline.position / multiplier, sequence.duration)
        self.editor.refresh()
        self.editor.fit_active()
        self._on_edited()

    def _on_smooth_selected(self):
        if self.session.sequence_state != SEQ_IDLE:
            return
        self.editor.smooth_selected()

    def _on_smooth_all(self):
        sequence = self.session.sequence
        if sequence is None or self.session.sequence_state != SEQ_IDLE:
            return
        sequence.smooth_all()
        self.editor.refresh()
        self._on_edited()

    def _describe_frame(self):
        track = self.editor.active_track
        frames = track.sorted_frames() if track is not None else []
        if track is None or self._selected_index is None \
                or not 0 <= self._selected_index < len(frames):
            self.frame_label.setText('No key point selected')
            self.interp_combo.setEnabled(False)
            self.smooth_selected_btn.setEnabled(False)
            return
        frame = frames[self._selected_index]
        selected = self.editor.selected_indices
        if len(selected) > 1:
            first, last = frames[selected[0]], frames[selected[-1]]
            self.frame_label.setText(
                f"{track.label}   {len(selected)} key points selected   "
                f"{first.t:.2f}–{last.t:.2f} s")
            kinds = {frames[index].interp for index in selected}
            if len(kinds) == 1:
                self.interp_combo.setCurrentIndex(
                    self.interp_combo.findData(next(iter(kinds))))
            else:
                self.interp_combo.setCurrentIndex(-1)
            self.interp_combo.setEnabled(
                self.session.sequence_state == SEQ_IDLE)
            self.smooth_selected_btn.setEnabled(
                self.session.sequence_state == SEQ_IDLE
                and any(index + 1 in selected for index in selected))
            return
        pinned = '  (pinned to the start)' if self._selected_index == 0 else ''
        self.frame_label.setText(
            f"{track.label}   t = {frame.t:.2f} s   "
            f"{frame.value:.3f} SLPM{pinned}")
        self.interp_combo.setEnabled(self.session.sequence_state == SEQ_IDLE)
        self.interp_combo.setCurrentIndex(
            self.interp_combo.findData(frame.interp))
        self.smooth_selected_btn.setEnabled(False)

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
        can_edit = idle and has_sequence
        self.slower_btn.setEnabled(can_edit)
        self.faster_btn.setEnabled(can_edit)
        self.smooth_all_btn.setEnabled(can_edit)
        self.smooth_selected_btn.setEnabled(
            can_edit and len(self.editor.selected_indices) > 1
            and any(index + 1 in self.editor.selected_indices
                    for index in self.editor.selected_indices))
        self._describe_frame()

    # -- painting -------------------------------------------------------- #
    def paintEvent(self, _event):
        paint_glass(self, radius=theme.RADIUS_CARD)
