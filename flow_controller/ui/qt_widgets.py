"""Reusable Qt widgets for the flow controller view layer.

These are the pieces the Tk version hand-draws onto ``tk.Canvas``
(``RoundedPanel``, ``ZenTabs``) or cannot do at all (the flow bar).  In Qt they
are ordinary widgets with a ``paintEvent``, so they get anti-aliasing, hover
state, layout participation and high-DPI scaling for free.

The surfaces are frosted glass.  Qt stylesheets have no ``backdrop-filter``, so
the effect is assembled by hand: ``GlassBackdrop`` paints one soft, grainy
wallpaper at the root of the window, and every panel above it is a translucent
rim-lit sheet drawn by ``paint_glass``.  Because the wallpaper is already a
smooth blur of colour, no per-panel blur pass is needed — which matters, since
this UI repaints live readings several times a second.

Note the ``=None`` defaults on every colour, radius and size parameter.  Theme
values must be read *inside* the call, never captured in a default argument:
defaults bind once at import, so a captured value would survive a re-theme as a
stale copy of the old palette.
"""

from __future__ import annotations

import random

from PySide6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRectF, Qt, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ..core import unit_prefs
from . import qt_theme as theme


# ---------------------------------------------------------------------- #
#  Small helpers                                                          #
# ---------------------------------------------------------------------- #
def mono(size=9, bold=False):
    font = QFont(theme.FONT_MONO_FAMILY)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(theme.font_pt(size))
    font.setBold(bold)
    return font


def label(text, *, color=None, size=9, bold=False, monospace=False,
          object_name=None):
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    font = mono(size, bold) if monospace else QFont(theme.FONT_UI_FAMILY)
    if not monospace:
        font.setPointSize(theme.font_pt(size))
        font.setBold(bold)
    widget.setFont(font)
    widget.setStyleSheet(
        f"color: {color or theme.TEXT}; background: transparent;")
    return widget


def field_grid(specs, *, columns=2, width=82):
    """``[(caption, default)]`` laid out as aligned label/entry pairs.

    Returns ``(layout, {caption: QLineEdit})``.  The captions are the keys
    because they are what the operator reads; a screen that calls a field
    "φ stage 1" and stores it under ``phi_s1`` gives the two names a chance
    to drift apart, and the one the code trusts is not the one on screen.
    """
    grid = QGridLayout()
    grid.setContentsMargins(0, 2, 0, 2)
    grid.setHorizontalSpacing(theme.PAD_MD)
    grid.setVerticalSpacing(theme.PAD_SM + 2)
    entries = {}
    for index, (caption, default) in enumerate(specs):
        line, column = divmod(index, columns)
        caption_label = QLabel(caption)
        caption_label.setObjectName('FieldLabel')
        entry = QLineEdit(str(default))
        entry.setFixedWidth(theme.scale(width))
        entry.setAlignment(Qt.AlignmentFlag.AlignRight)
        grid.addWidget(caption_label, line, column * 2)
        grid.addWidget(entry, line, column * 2 + 1)
        entries[caption] = entry
    for column in range(columns):
        grid.setColumnStretch(column * 2, 1)
    return grid, entries


def row(*widgets, spacing=None, margins=(0, 0, 0, 0), stretch_at=None):
    """Widgets side by side.  ``None`` in the list becomes a stretch."""
    # spacing defaults to None rather than to the theme value: a default
    # argument binds once at import and would survive a re-theme as a stale
    # copy of the old spacing scale.
    container = QWidget()
    container.setObjectName('Row')
    layout = QHBoxLayout(container)
    layout.setContentsMargins(*margins)
    layout.setSpacing(theme.PAD_SM if spacing is None else spacing)
    for index, widget in enumerate(widgets):
        if widget is None:
            layout.addStretch(1)
        else:
            layout.addWidget(widget, 1 if index == stretch_at else 0)
    return container


def divider():
    """A hairline rule.  Translucent, like everything else on the sheet — an
    opaque line reads as a scratch on the glass."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet('background-color: rgba(255, 255, 255, 20);'
                       ' border: none;')
    return line


# ---------------------------------------------------------------------- #
#  Glass                                                                  #
# ---------------------------------------------------------------------- #
_GRAIN = None


def grain_tile(size=128):
    """A cached tile of white noise — what makes a translucent sheet read as
    *frosted* rather than merely see-through."""
    global _GRAIN
    if _GRAIN is None:
        data = random.randbytes(size * size)
        image = QImage(data, size, size, size,
                       QImage.Format.Format_Grayscale8)
        # copy() detaches from the Python buffer, which is about to be freed.
        _GRAIN = QPixmap.fromImage(image.copy())
    return _GRAIN


def paint_glass(widget, *, radius=None, tint=None, rim=None, rim_high=None,
                sheen=True, painter=None):
    """Draw one frosted sheet filling ``widget``.

    Three layers, in order: a translucent body tint, a top-down sheen so the
    face catches light, and a rim that is bright at the top-left and fades away
    to the bottom-right.  The rim is what actually sells the effect — a flat
    border reads as a box, a graded one reads as an edge.
    """
    radius = theme.RADIUS_CARD if radius is None else radius
    tint = theme.GLASS_TINT if tint is None else tint
    rim = theme.GLASS_RIM if rim is None else rim
    rim_high = theme.GLASS_RIM_HI if rim_high is None else rim_high

    owned = painter is None
    painter = painter or QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    rect = QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    painter.fillPath(path, QColor(*tint))

    if sheen:
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(*theme.GLASS_SHEEN))
        gradient.setColorAt(0.35, QColor(255, 255, 255, 5))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, QBrush(gradient))

    edge = QLinearGradient(rect.topLeft(), rect.bottomRight())
    edge.setColorAt(0.0, QColor(*rim_high))
    edge.setColorAt(0.45, QColor(*rim))
    edge.setColorAt(1.0, QColor(*theme.GLASS_RIM_LO))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QBrush(edge), 1))
    painter.drawPath(path)

    if owned:
        painter.end()
    return path


class GlassBackdrop(QWidget):
    """The wallpaper every glass panel sits on.

    A dark base gradient, a few very dim colour blobs and a grain overlay,
    rendered once into a pixmap and reused until the window resizes.  The blobs
    are what the glass has to refract; without them translucency is invisible.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache = None
        self.setAutoFillBackground(False)

    def resizeEvent(self, event):
        self._cache = None
        super().resizeEvent(event)

    def paintEvent(self, _event):
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self._render()
        QPainter(self).drawPixmap(0, 0, self._cache)

    def _render(self):
        width, height = max(self.width(), 1), max(self.height(), 1)
        pixmap = QPixmap(width, height)
        pixmap.fill(QColor(theme.BG))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        top, middle, bottom = theme.BACKDROP_BASE
        base = QLinearGradient(0, 0, width * 0.4, height)
        base.setColorAt(0.0, QColor(top))
        base.setColorAt(0.55, QColor(middle))
        base.setColorAt(1.0, QColor(bottom))
        painter.fillRect(0, 0, width, height, base)

        span = max(width, height)
        for x, y, extent, color, alpha in theme.BACKDROP_BLOBS:
            blob = QRadialGradient(width * x, height * y, span * extent)
            inner, outer = QColor(color), QColor(color)
            inner.setAlpha(int(alpha))
            outer.setAlpha(0)
            blob.setColorAt(0.0, inner)
            blob.setColorAt(1.0, outer)
            painter.fillRect(0, 0, width, height, blob)

        if theme.GRAIN_OPACITY > 0:
            painter.setOpacity(theme.GRAIN_OPACITY)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Overlay)
            painter.fillRect(0, 0, width, height, QBrush(grain_tile()))
        painter.end()
        return pixmap


class GlassBar(QWidget):
    """Chrome that spans the full width — title bar, status bar.

    Square, denser than a card, and separated by a single hairline rather than
    a full rim, so it reads as the frame of the window instead of one more
    floating panel.
    """

    def __init__(self, edge='bottom', parent=None):
        super().__init__(parent)
        self._edge = edge

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(*theme.GLASS_TINT_BAR))

        sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 12))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(rect, QBrush(sheen))

        painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
        y = rect.bottom() if self._edge == 'bottom' else rect.top()
        painter.drawLine(rect.left(), y, rect.right(), y)
        painter.end()


class StatusDot(QWidget):
    """A filled dot with an optional soft halo, for live state indication."""

    def __init__(self, color=None, diameter=8, parent=None):
        super().__init__(parent)
        self._color = QColor(color or theme.TEXT_DIM)
        self._diameter = diameter
        self.setFixedSize(diameter + 8, diameter + 8)

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = self.rect().center()
        painter.setPen(Qt.PenStyle.NoPen)
        # Two halo rings rather than one: on glass a single flat halo looks
        # like a sticker, a graded one looks lit.
        for radius, alpha in ((self._diameter / 2 + 4, 30),
                              (self._diameter / 2 + 2, 60)):
            halo = QColor(self._color)
            halo.setAlpha(alpha)
            painter.setBrush(halo)
            painter.drawEllipse(centre, radius, radius)
        painter.setBrush(self._color)
        painter.drawEllipse(centre, self._diameter / 2, self._diameter / 2)


# ---------------------------------------------------------------------- #
#  Collapsible card                                                       #
# ---------------------------------------------------------------------- #
class Card(QFrame):
    """A titled, optionally collapsible glass panel — the Qt form of RoundedPanel.

    The Tk original paints its rounded border onto a canvas and toggles
    visibility instantly.  Here the sheet is painted with a graded rim and the
    collapse is animated, which makes it read as one surface opening rather than
    the layout jumping.
    """

    toggled = Signal(bool)

    def __init__(self, title, *, index=None, collapsible=True,
                 collapsed=False, parent=None):
        super().__init__(parent)
        self.setObjectName('Card')
        self._collapsible = collapsible

        pad = theme.CARD_PAD
        outer = QVBoxLayout(self)
        outer.setContentsMargins(pad, pad - 2, pad, pad)
        outer.setSpacing(theme.PAD_MD)

        header = QWidget()
        header.setObjectName('CardHeader')
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(theme.PAD_SM + 2)
        self._badge = QLabel('' if index is None else str(index))
        self._badge.setObjectName('CardIndex')
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setVisible(index is not None)
        header_layout.addWidget(self._badge)
        title_label = QLabel(title)
        title_label.setObjectName('CardTitle')
        header_layout.addWidget(title_label)
        header_layout.addStretch(1)

        self._chevron = QLabel('▾' if not collapsed else '▸')
        self._chevron.setObjectName('CardChevron')
        if collapsible:
            header_layout.addWidget(self._chevron)
            header.setCursor(Qt.CursorShape.PointingHandCursor)
            header.mousePressEvent = self._header_clicked
        outer.addWidget(header)

        self.body = QWidget()
        self.body.setObjectName('CardBody')
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(theme.FIELD_GAP)
        outer.addWidget(self.body)

        self._animation = QPropertyAnimation(self.body, b'maximumHeight', self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._animation.finished.connect(self._animation_finished)

        self._collapsed = False
        if collapsed:
            self.set_collapsed(True, animate=False)

    def paintEvent(self, _event):
        paint_glass(self, radius=theme.RADIUS_CARD)

    def add(self, widget, stretch=0):
        self.body_layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout):
        self.body_layout.addLayout(layout)
        return layout

    def add_spacing(self, pixels):
        self.body_layout.addSpacing(pixels)

    def is_collapsed(self):
        return self._collapsed

    def set_index(self, index):
        """Renumber the step badge, or drop it with ``None``.

        The badges are a sequence the operator is meant to work down, so when
        a mode hides some of the steps the ones that are left have to close
        the gap.  A lone "3" on screen invites a hunt for the missing 1 and 2.
        """
        self._badge.setText('' if index is None else str(index))
        self._badge.setVisible(index is not None)

    def _header_clicked(self, _event):
        self.set_collapsed(not self._collapsed)

    def _animation_finished(self):
        # Release the cap once open, or the body can never grow past the height
        # it happened to have when it was expanded.
        if not self._collapsed:
            self.body.setMaximumHeight(16777215)

    def set_collapsed(self, collapsed, animate=True):
        if not self._collapsible:
            return
        self._collapsed = collapsed
        self._chevron.setText('▸' if collapsed else '▾')
        target = 0 if collapsed else self.body.sizeHint().height()
        if not animate:
            self.body.setMaximumHeight(target)
            if not collapsed:
                self.body.setMaximumHeight(16777215)
            self.toggled.emit(not collapsed)
            return
        self._animation.stop()
        self._animation.setStartValue(self.body.height())
        self._animation.setEndValue(target)
        self._animation.start()
        self.toggled.emit(not collapsed)


# ---------------------------------------------------------------------- #
#  Stage header                                                           #
# ---------------------------------------------------------------------- #
class StageHeader(QWidget):
    """The rule that separates one burner stage from the next.

    Controllers are grouped by stage because that is how the rig is operated —
    an operator reasons about "stage 2 is lean", not about "unit 5".  The header
    carries the stage's own fuel total so the grouping is doing work, not just
    drawing a line.
    """

    def __init__(self, title, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.PAD_XS, theme.PAD_SM,
                                  theme.PAD_XS, theme.PAD_XS)
        layout.setSpacing(theme.PAD_MD)

        layout.addWidget(label(title.upper(), color=theme.TEXT_BRIGHT, size=9,
                               bold=True, object_name='SectionLabel'))

        self._rule = QFrame()
        self._rule.setFixedHeight(1)
        self._rule.setStyleSheet('background-color: rgba(255, 255, 255, 22);'
                                 ' border: none;')
        self._rule.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Fixed)
        layout.addWidget(self._rule, 1)

        self._summary = label('', color=theme.TEXT_DIM, size=8, monospace=True)
        layout.addWidget(self._summary)

    def set_summary(self, text):
        self._summary.setText(text)


# ---------------------------------------------------------------------- #
#  Flow bar                                                               #
# ---------------------------------------------------------------------- #
class FlowBar(QWidget):
    """Actual flow against its setpoint, on a shared track.

    The filled portion is the live reading; the notch is the commanded
    setpoint.  Colour reflects the gap between them, so an operator can see a
    controller failing to track without reading any numbers.  There is no
    practical Tk equivalent short of another hand-managed canvas.
    """

    def __init__(self, color=None, parent=None):
        super().__init__(parent)
        self._color = QColor(color or theme.OK)
        self._value = 0.0
        self._setpoint = 0.0
        self._maximum = 1.0
        self.setFixedHeight(7)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set_state(self, value, setpoint, maximum):
        self._value = max(0.0, float(value))
        self._setpoint = max(0.0, float(setpoint))
        self._maximum = max(float(maximum), self._setpoint, self._value, 1e-6)
        self.update()

    def _tracking_color(self):
        if self._setpoint <= 1e-6:
            return QColor(theme.TEXT_DIM) if self._value <= 1e-6 else self._color
        error = abs(self._value - self._setpoint) / self._setpoint
        if error > 0.10:
            return QColor(theme.DANGER_HOVER)
        if error > 0.03:
            return QColor(theme.WARN)
        return self._color

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        radius = rect.height() / 2

        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QColor(0, 0, 0, 110))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 16), 1))
        painter.drawPath(track)

        width = rect.width()
        if self._maximum > 0 and self._value > 0:
            filled = min(width, width * self._value / self._maximum)
            if filled > 1:
                color = self._tracking_color()
                fill = QPainterPath()
                fill.addRoundedRect(QRectF(0, 0, filled, rect.height()),
                                    radius, radius)
                # A little vertical shading keeps the fill from looking like a
                # flat sticker laid over the glass.
                gradient = QLinearGradient(0, 0, 0, rect.height())
                bright = QColor(color).lighter(118)
                gradient.setColorAt(0.0, bright)
                gradient.setColorAt(1.0, color)
                painter.fillPath(fill, QBrush(gradient))

        if self._setpoint > 0:
            x = min(width - 1.5, width * self._setpoint / self._maximum)
            painter.setPen(QPen(QColor(theme.TEXT_BRIGHT), 1.5))
            painter.drawLine(QRectF(x, 0, 0, rect.height()).topLeft(),
                             QRectF(x, 0, 0, rect.height()).bottomLeft())


# ---------------------------------------------------------------------- #
#  Unit card                                                              #
# ---------------------------------------------------------------------- #
def _reading(value, decimals, width):
    """Format one live number, or a dash where there is no number."""
    if value is None:
        return '—'.rjust(width)
    return f'{float(value):{width}.{decimals}f}'


class UnitCard(QFrame):
    """One controller: identity, live readings, tracking bar, setpoint entry.

    The gas is identified by the colour of its name and of its tracking bar.
    There is no coloured edge stripe: once the cards are grouped under stage
    headers, a stripe on every row is a third redundant encoding of the same
    fact, and it fights the rim that makes the sheet read as glass.
    """

    #: ``object`` rather than ``str`` because a unit id is whatever the caller
    #: addresses controllers by -- a device letter on the rig, a number in the
    #: layout spike -- and coercing it here would hand back a key that no
    #: longer matches the dictionary it came out of.
    setpoint_requested = Signal(object, float)
    #: Unit and the full scale the operator typed, ``0.0`` meaning automatic.
    full_scale_requested = Signal(object, float)
    #: Unit and the ramp rate the operator typed in SLPM/s, ``0.0`` for none.
    ramp_rate_requested = Signal(object, float)
    #: Unit and whether the operator has turned this line's ramping off.
    ramp_off_requested = Signal(object, bool)

    def __init__(self, unit, gas_name, color, full_scale, caption=None,
                 declared_scale=None, declared_ramp=None,
                 declared_ramp_off=False, parent=None):
        super().__init__(parent)
        self._unit = unit
        self._full_scale = full_scale
        #: The operator's own figure for this meter, or ``None`` to keep
        #: scaling the bar from what the run has asked for.
        self._declared_scale = declared_scale
        self._scale_guard = False
        self._ramp_guard = False
        self._accent = QColor(color)
        self._hover = False
        self.setObjectName('UnitCard')
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # A reading row is not padding to be reclaimed.  Left to a Preferred
        # policy the layout will shrink it whenever the column runs short of
        # height -- which happens as soon as the base font is raised -- and it
        # takes the shortfall out of the setpoint entry and its button, cutting
        # the glyphs in half.  Fixed makes the row keep its hint and lets the
        # surrounding scroll area absorb the overflow.
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, theme.PAD_MD, 0)
        row.setSpacing(0)

        identity = QVBoxLayout()
        identity.setContentsMargins(theme.PAD_MD + 2, theme.PAD_MD,
                                    theme.PAD_MD, theme.PAD_MD)
        identity.setSpacing(2)
        identity.addWidget(label(gas_name, color=color, size=10, bold=True))
        self._caption = label(
            caption if caption is not None else f'Unit {unit}  ·  {full_scale:g} SLPM',
            color=theme.TEXT_DIM, size=8, monospace=True)
        identity.addWidget(self._caption)
        identity_holder = QWidget()
        identity_holder.setLayout(identity)
        identity_holder.setFixedWidth(theme.scale(150))
        row.addWidget(identity_holder)

        # Flow: the number the operator actually watches.
        flow_block = QVBoxLayout()
        flow_block.setContentsMargins(0, theme.PAD_MD, 0, theme.PAD_MD)
        flow_block.setSpacing(theme.PAD_XS + 1)
        flow_row = QHBoxLayout()
        flow_row.setSpacing(5)
        flow_row.setContentsMargins(0, 0, 0, 0)
        self._flow = label('0.000', color=theme.TEXT_BRIGHT, size=17, bold=True,
                           monospace=True)
        flow_row.addWidget(self._flow)
        flow_row.addWidget(label('SLPM', color=theme.TEXT_DIM, size=8))
        flow_row.addStretch(1)
        flow_block.addLayout(flow_row)
        self._bar = FlowBar(color)
        flow_block.addWidget(self._bar)

        # The things the instrument cannot tell us, declared per meter and
        # remembered between runs: how far its bar reaches, and how fast -- or
        # whether -- its line is paced.  They sit under the bar rather than out
        # among the readings because the reading columns cannot scroll sideways
        # -- a control parked there widens every card, and past the column
        # width there is no way to reach it.  One row, scale then ramp, so the
        # block reads as a single line of settings under the bar rather than a
        # stack that pushes the card taller; spin boxes rather than a dialog,
        # because the full scale is on the sticker on the front of the meter
        # and reading it off should be one gesture.
        prefs_grid = QGridLayout()
        prefs_grid.setHorizontalSpacing(5)
        prefs_grid.setVerticalSpacing(2)
        prefs_grid.setContentsMargins(0, 1, 0, 0)

        prefs_grid.addWidget(label('FULL SCALE', color=theme.TEXT_DIM, size=7),
                             0, 0)
        self.scale_spin = QDoubleSpinBox()
        # Same ceiling as the store applies, so a value the box accepts is
        # never quietly clamped on its way to disk.
        self.scale_spin.setRange(0.0, unit_prefs.MAX_FULL_SCALE)
        self.scale_spin.setDecimals(1)
        self.scale_spin.setSingleStep(1.0)
        # Zero is not a scale, so it is the way to ask for the old behaviour:
        # the bar spanning the largest thing the line has been asked for.
        self.scale_spin.setSpecialValueText('auto')
        self.scale_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.scale_spin.setValue(float(declared_scale or 0.0))
        self.scale_spin.setToolTip(
            "This meter's full scale in SLPM, which sets the span of the "
            "tracking bar. The controllers do not report it, so it is "
            "remembered per unit between runs. 'auto' scales the bar from the "
            "largest flow or setpoint this line has been asked for instead.")
        self.scale_spin.valueChanged.connect(self._emit_full_scale)
        prefs_grid.addWidget(self.scale_spin, 0, 1)
        prefs_grid.addWidget(label('SLPM', color=theme.TEXT_DIM, size=7), 0, 2)

        ramp_caption = label('RAMP', color=theme.TEXT_DIM, size=7)
        # The gap that separates the two settings.  A spacer column would have
        # to be given a minimum width, and a fixed column in this grid is what
        # widened every card the last time this block was laid out.
        ramp_caption.setContentsMargins(theme.PAD_MD, 0, 0, 0)
        prefs_grid.addWidget(ramp_caption, 0, 3)
        self.ramp_spin = QDoubleSpinBox()
        self.ramp_spin.setRange(0.0, unit_prefs.MAX_RAMP_RATE)
        self.ramp_spin.setDecimals(2)
        self.ramp_spin.setSingleStep(0.5)
        # No rate is the honest default: the controller is told the setpoint and
        # goes there at whatever pace its own valve manages.
        self.ramp_spin.setSpecialValueText('step')
        self.ramp_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.ramp_spin.setValue(float(declared_ramp or 0.0))
        self.ramp_spin.setToolTip(
            "How fast this line may move, in SLPM per second. Every setpoint "
            "this application writes to the unit is walked at this rate -- "
            "typed here, driven by the ignition sequence, or replayed from a "
            "recording. 'step' writes the setpoint in one go and lets the "
            "controller travel at its own pace.")
        self.ramp_spin.valueChanged.connect(self._emit_ramp_rate)
        prefs_grid.addWidget(self.ramp_spin, 0, 4)
        prefs_grid.addWidget(label('SLPM/s', color=theme.TEXT_DIM, size=7),
                             0, 5)

        # Ramping off outright, which is not the same as no rate: with no rate
        # this application still walks the pilot and the two air lines over a
        # minimum move time, because a step edge on those is a flame risk.  Off
        # defeats that as well, so it is a latch that goes red rather than
        # another number, and the rate box greys out beside it to say plainly
        # that whatever figure is in it is not in force.
        self.ramp_off_btn = QPushButton('OFF')
        self.ramp_off_btn.setCheckable(True)
        self.ramp_off_btn.setProperty('density', 'compact')
        self.ramp_off_btn.setFixedWidth(theme.scale(38))
        self.ramp_off_btn.setToolTip(
            "Turn ramping off for this controller altogether. Setpoints are "
            "then written in one go whatever rate is typed beside this -- "
            "including on the pilot and the two air lines, which are never "
            "otherwise stepped. Remembered between runs.")
        self.ramp_off_btn.toggled.connect(self._on_ramp_off_toggled)
        prefs_grid.addWidget(self.ramp_off_btn, 0, 6)
        self.set_ramp_disabled(declared_ramp_off)

        # Left to their own hints the two boxes are wide enough to push the
        # card past the width of the column it lives in, and neither operation
        # column scrolls sideways.  A minimum they may shrink to, a maximum
        # they may not grow past, and the stretch beyond the last column, so
        # spare width goes to the empty space rather than into the controls.
        for spin in (self.scale_spin, self.ramp_spin):
            spin.setMinimumWidth(theme.scale(66))
            spin.setMaximumWidth(theme.scale(84))
        prefs_grid.setColumnStretch(7, 1)
        flow_block.addLayout(prefs_grid)

        flow_holder = QWidget()
        flow_holder.setLayout(flow_block)
        flow_holder.setMinimumWidth(theme.scale(150))
        row.addWidget(flow_holder, 1)

        self._readings = {}
        for key, caption, suffix in (('setpoint', 'SP', 'SLPM'),
                                     ('pressure', 'PRESS', 'psia'),
                                     ('temp', 'TEMP', '°C')):
            block = QVBoxLayout()
            block.setContentsMargins(theme.PAD_LG, theme.PAD_MD + 2,
                                     0, theme.PAD_MD + 2)
            block.setSpacing(3)
            block.addWidget(label(caption, color=theme.TEXT_DIM, size=7))
            value_row = QHBoxLayout()
            value_row.setSpacing(4)
            value_row.setContentsMargins(0, 0, 0, 0)
            value = label('0.000', color=theme.TEXT_MUTED, size=10,
                          monospace=True)
            value_row.addWidget(value)
            value_row.addWidget(label(suffix, color=theme.TEXT_DIM, size=7))
            value_row.addStretch(1)
            block.addLayout(value_row)
            holder = QWidget()
            holder.setLayout(block)
            holder.setFixedWidth(theme.scale(102))
            row.addWidget(holder)
            self._readings[key] = value

        entry_block = QVBoxLayout()
        entry_block.setContentsMargins(theme.PAD_LG + 4, theme.PAD_MD,
                                       0, theme.PAD_MD)
        entry_block.setSpacing(theme.PAD_XS + 1)
        entry_block.addWidget(label('SETPOINT', color=theme.AMBER, size=7,
                                    bold=True))
        entry_row = QHBoxLayout()
        entry_row.setSpacing(6)
        entry_row.setContentsMargins(0, 0, 0, 0)
        self.entry = QLineEdit('0')
        self.entry.setFixedWidth(theme.scale(76))
        self.entry.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.entry.returnPressed.connect(self._emit_setpoint)
        entry_row.addWidget(self.entry)
        send = QPushButton('Set')
        send.setProperty('variant', 'accent')
        send.setProperty('density', 'compact')
        send.setFixedWidth(theme.scale(54))
        # Fixed vertical policy, not a fixed height in pixels.  The row's height
        # is set by whichever column is tallest, and a default policy lets the
        # button be squeezed until its label is clipped -- but a height measured
        # here would be worse: the widget has no parent yet, so it has not been
        # polished against the stylesheet, and its size hint still reflects the
        # unstyled default font rather than the theme's.  Fixed policy makes the
        # layout honour whatever the hint turns out to be once it is styled.
        send.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        send.clicked.connect(self._emit_setpoint)
        entry_row.addWidget(send)
        entry_block.addLayout(entry_row)
        entry_holder = QWidget()
        entry_holder.setLayout(entry_block)
        row.addWidget(entry_holder)

        self._dot = StatusDot(theme.TEXT_DIM)
        row.addSpacing(theme.PAD_SM)
        row.addWidget(self._dot)

    # -- painting -------------------------------------------------------
    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        rim = (255, 255, 255, 44) if self._hover else theme.GLASS_RIM
        paint_glass(self, radius=theme.RADIUS_PANEL,
                    tint=theme.GLASS_TINT_SOFT, rim=rim)

    # -- behaviour ------------------------------------------------------
    def _emit_setpoint(self):
        try:
            value = float(self.entry.text())
        except ValueError:
            self.entry.setStyleSheet(f"border-color: {theme.DANGER};")
            return
        self.entry.setStyleSheet('')
        self.setpoint_requested.emit(self._unit, value)

    def _emit_full_scale(self, value):
        if self._scale_guard:
            return
        self._declared_scale = float(value) or None
        self.full_scale_requested.emit(self._unit, float(value))
        self.update()

    def _emit_ramp_rate(self, value):
        if self._ramp_guard:
            return
        self.ramp_rate_requested.emit(self._unit, float(value))

    def _on_ramp_off_toggled(self, checked):
        self._apply_ramp_off(bool(checked))
        if self._ramp_guard:
            return
        self.ramp_off_requested.emit(self._unit, bool(checked))

    def _apply_ramp_off(self, off):
        """Dress the ramp controls for the state they are actually in."""
        self.ramp_spin.setEnabled(not off)
        # Red while ramping is off: this is the one control on the card that
        # takes a protection away rather than changing a number, and it should
        # not look like the rest of the settings while it is doing that.
        self.ramp_off_btn.setProperty('variant', 'danger' if off else 'quiet')
        # A property a stylesheet selects on is read at polish time, so the
        # widget has to be told to look again.
        self.ramp_off_btn.style().unpolish(self.ramp_off_btn)
        self.ramp_off_btn.style().polish(self.ramp_off_btn)

    def set_caption(self, text):
        """Replace the line under the gas name."""
        self._caption.setText(text)

    def set_declared_scale(self, scale):
        """Show a full scale that was set elsewhere, without re-announcing it."""
        self._declared_scale = None if not scale else float(scale)
        self._scale_guard = True
        try:
            self.scale_spin.setValue(float(scale or 0.0))
        finally:
            self._scale_guard = False

    def set_declared_ramp(self, rate):
        """Show a ramp rate that was set elsewhere, without re-announcing it."""
        self._ramp_guard = True
        try:
            self.ramp_spin.setValue(float(rate or 0.0))
        finally:
            self._ramp_guard = False

    def set_ramp_disabled(self, off):
        """Show a ramp-off state set elsewhere, without re-announcing it."""
        self._ramp_guard = True
        try:
            self.ramp_off_btn.setChecked(bool(off))
        finally:
            self._ramp_guard = False
        # ``setChecked`` only fires when the state actually changes, and this
        # is also the call that dresses a card built already off.
        self._apply_ramp_off(bool(off))

    def set_scale(self, scale):
        """Re-scale the tracking bar from what the run is asking for.

        The devices do not report their full scale over the wire, so unless the
        operator has declared one this bar is scaled from what this rig is
        actually being asked for -- the stored target, or the largest setpoint
        seen -- rather than from a figure invented here.
        :meth:`FlowBar.set_state` floors the span by the live reading anyway, so
        an under-estimate corrects itself on the next pass instead of drawing a
        bar that runs off the end.
        """
        self._full_scale = max(float(scale), 1e-6)

    def bar_scale(self):
        """The span the bar is drawn against: declared if there is one."""
        if self._declared_scale:
            return max(float(self._declared_scale), 1e-6)
        return max(float(self._full_scale), 1e-6)

    def update_readings(self, flow, setpoint, pressure, temp, *, live=True):
        """Show one pass.  ``None`` means the device did not answer.

        A reading that failed is drawn as a dash rather than as zero.  Zero is
        a legitimate measurement on this rig -- a closed controller reads
        exactly that -- so printing it for a line that said nothing would put a
        plausible number in front of the operator with nothing behind it.
        """
        self._flow.setText(_reading(flow, 3, 7))
        self._readings['setpoint'].setText(_reading(setpoint, 3, 7))
        self._readings['pressure'].setText(_reading(pressure, 3, 7))
        self._readings['temp'].setText(_reading(temp, 2, 6))
        self._bar.set_state(float(flow or 0.0), float(setpoint or 0.0),
                            self.bar_scale())
        setpoint = float(setpoint or 0.0)
        flow = float(flow or 0.0)
        if not live:
            self._dot.set_color(theme.TEXT_DIM)
        elif setpoint > 1e-6 and abs(flow - setpoint) / setpoint > 0.10:
            self._dot.set_color(theme.DANGER_HOVER)
        elif setpoint > 1e-6:
            self._dot.set_color(theme.OK)
        else:
            self._dot.set_color(theme.TEXT_DIM)


# ---------------------------------------------------------------------- #
#  Metric tile                                                            #
# ---------------------------------------------------------------------- #
class MetricTile(QFrame):
    """A single derived number — a stored target, a total, an equivalence ratio.

    Tiles sit *on* a card, so they are lit from above rather than tinted dark:
    glass on glass, not a second pane of the same glass.
    """

    def __init__(self, caption, *, color=None, size=11, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.PAD_SM + 2, theme.PAD_SM,
                                  theme.PAD_SM + 2, theme.PAD_SM)
        layout.setSpacing(3)
        caption_label = label(caption, color=theme.TEXT_DIM, size=7)
        caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(caption_label)
        self.value = label('--', color=color or theme.AMBER, size=size,
                           bold=True, monospace=True)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value)

    def paintEvent(self, _event):
        paint_glass(self, radius=theme.RADIUS_TILE, tint=theme.GLASS_INSET,
                    rim=(255, 255, 255, 18), rim_high=(255, 255, 255, 38))

    def set_value(self, text):
        self.value.setText(text)


class StateBanner(QFrame):
    """The ignition state line, colour-coded to the current phase."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.PAD_MD + 2, theme.PAD_MD,
                                  theme.PAD_MD + 2, theme.PAD_MD)
        layout.setSpacing(theme.PAD_SM + 2)
        self._dot = StatusDot(theme.TEXT_MUTED, diameter=7)
        layout.addWidget(self._dot)
        self._text = label('IDLE — calculate targets first',
                           color=theme.TEXT_MUTED, size=9, bold=True,
                           monospace=True)
        layout.addWidget(self._text)
        layout.addStretch(1)
        self._color = QColor(theme.TEXT_MUTED)
        self._state = ('idle', 'IDLE — calculate targets first')
        self.set_state(*self._state)

    def state(self):
        """The current ``(kind, text)`` — so a rebuilt view can be put back
        into the phase the rig is actually in."""
        return self._state

    def paintEvent(self, _event):
        # Tinted glass: the phase colour bled into the sheet rather than a
        # solid block, so the banner still belongs to the surface behind it.
        tint = QColor(self._color)
        tint.setAlpha(34)
        rim = QColor(self._color)
        rim.setAlpha(120)
        paint_glass(self, radius=theme.RADIUS_TILE,
                    tint=(tint.red(), tint.green(), tint.blue(), tint.alpha()),
                    rim=(rim.red(), rim.green(), rim.blue(), 90),
                    rim_high=(rim.red(), rim.green(), rim.blue(), 165))

    def set_state(self, kind, text):
        colors = {
            'idle': theme.TEXT_MUTED,
            'ready': theme.WARN,
            'running': theme.OK,
            'fault': '#f87171',
        }
        color = colors.get(kind, colors['idle'])
        self._state = (kind, text)
        self._color = QColor(color)
        self._dot.set_color(color)
        self._text.setStyleSheet(f"color: {color}; background: transparent;")
        self._text.setText(text)
        self.update()
